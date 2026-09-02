"""LLM-based fallback miner for NF-Validation summary report PDFs.

scrapers/summary_report_parser.py's deterministic pdfplumber pipeline mines a
real per-category performance breakdown for only 50/146 (34%) of the current
NF-Validation reports -- not because the underlying information differs
(ISO 16140-2 mandates what values a validation study must report), but
because there is no mandated PDF *layout*, and each accredited lab uses its
own report template. Confirmed real blockers pdfplumber's structural
table-detection can't work around: borderless/complex layouts pdfplumber
doesn't even recognize as a table, and at least one report whose "D-bar
(bias)" symbol renders as garbled Unicode glyphs that defeat ANY text-based
read (not a regex problem -- the PDF's own font encoding is broken for that
character).

This module sends Claude the actual PDF as a native document input (not
pre-extracted text), so it reads the true rendered page directly -- robust to
both the broken-font case and to each lab's own header wording, which is
exactly the kind of semantic mapping ("which column is bias vs. SD, however
it's labeled") a deterministic parser has to hand-code per template and an
LLM does natively.

Deliberately a SEPARATE fallback path, not a replacement for the existing
pipeline: the pdfplumber path is free, fast, fully deterministic and
auditable, and already works for a third of reports -- there's no reason to
pay for or introduce LLM non-determinism there. See mine_hybrid() below,
which runs the deterministic path first and only calls Claude for whatever
it didn't get.

Status as of 2026-08-27 (budget frozen -- see below): a real 25-record
backfill batch wrote 2 clean records, then ran out of API credit; 5 of the
first 7 attempts came back as a placeholder/empty extraction despite the
anti-placeholder instructions below. Free diagnostics (scrapers/
diagnose_llm_failures.py, no API key -- see .github/workflows/
diagnose_llm_failures.yml) ruled out encryption (all 5 decrypt fine, same
as the fix below) and page count (the 2 successes were 96/102 pages, longer
than the shortest failure at 59) as causes. What the failures have in
common, confirmed by reading the actual extracted page text:
  - 4/5 are Petrifilm quantitative (enumeration) reports that bundle THREE
    separate relative-trueness tables for the SAME category set in one PDF
    (an initial validation study, a renewal/extension study, and a reduced
    automated-vs-manual-reading study) under different table numbers and
    headers ("Table 4", "Table 15"/"Table 21" via Bland-Altman difference
    plots) -- genuinely ambiguous which is "the" primary result, unlike the
    calibration sample's single-table reports. The two successful reports
    in this same batch faced a similar primary-vs-extension-study choice
    and resolved it correctly (see their traceability.notes), so the
    model CAN do this -- it isn't reliable at it yet.
  - The 5th (an older 2014 "Synt-" document, differently templated from
    every "_SR_" report) has one single, unambiguous relative-trueness
    table, but its header uses the same bolded "D-bar" (bias) math symbol
    already flagged above as breaking pdfplumber's text-based read on a
    different report -- plausible this also confuses a model's read of the
    table, though unconfirmed without another paid call.
Fixing either needs a validated prompt/schema change (e.g. explicit
tie-breaking rules for multiple same-category tables, and reassurance that
a garbled header symbol doesn't block reading the row underneath), which
in turn needs paid runs to test -- put on hold at the user's explicit
instruction after this batch spent ~$25 without clearing the backlog and
before further budget is approved. Do not resume backfill runs until that
happens; ship a validated prompt change first, calibrated the same way
the original 5-report pilot was (see validate_llm_miner.py).

Requires a real Anthropic API key (console.anthropic.com -- separate from a
claude.ai chat subscription, which does not include API access) in the
ANTHROPIC_API_KEY environment variable.

Usage (offline, from a saved summary-report PDF):
    python3 llm_report_miner.py --pdf path/to/report.pdf
"""
import argparse
import base64
import io
import json
import os
import re
import sys
from pathlib import Path

import anthropic
import pypdf

MODEL = "claude-sonnet-5"

# Mirrors schema/method.schema.json's `performance` object, restricted to the
# fields this project currently mines (interlaboratory_study and
# inclusivity/exclusivity aren't attempted here yet -- narrower scope than
# the full schema on purpose, easier to widen later than to have shipped an
# over-broad first version). additionalProperties: false + required listing
# every key on every object (including nullable ones) is Anthropic's strict
# tool-use requirement, not a project style choice.
_CATEGORY_ROW_QUALITATIVE = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "ISO 16140-2 food category name exactly as printed in the report."},
        "sensitivity_alternative_pct": {"type": ["number", "null"]},
        "sensitivity_reference_pct": {"type": ["number", "null"]},
        "relative_trueness_pct": {"type": ["number", "null"]},
        "false_positive_ratio_alternative_pct": {"type": ["number", "null"]},
    },
    "required": ["category", "sensitivity_alternative_pct", "sensitivity_reference_pct",
                 "relative_trueness_pct", "false_positive_ratio_alternative_pct"],
    "additionalProperties": False,
}

_CATEGORY_ROW_QUANTITATIVE = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "ISO 16140-2 food category name exactly as printed in the report."},
        "bias_log": {"type": ["number", "null"], "description": "Relative trueness / bias, in log10 units."},
        "sd_log": {"type": ["number", "null"]},
        "n_samples": {"type": ["integer", "null"]},
        "lower_limit_95": {"type": ["number", "null"]},
        "upper_limit_95": {"type": ["number", "null"]},
    },
    "required": ["category", "bias_log", "sd_log", "n_samples", "lower_limit_95", "upper_limit_95"],
    "additionalProperties": False,
}

RECORD_PERFORMANCE_TOOL = {
    "name": "record_performance_data",
    "description": (
        "Record the certificate identity and per-category validation-study performance data "
        "found in this NF-Validation summary report PDF, per EN ISO 16140-2:2016."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "certificate_number": {
                "type": ["string", "null"],
                "description": "The certificate number printed on the cover page, e.g. '3M 01/09-04/03A'. Null if genuinely not found.",
            },
            "method_nature": {
                # Plain "string" + enum, not a ["string","null"] type array --
                # confirmed against a real API 400 that Claude's strict
                # tool-schema validator rejects an enum paired with a
                # nullable type array (even with null itself listed in the
                # enum). "unknown" is the sentinel for "couldn't determine",
                # mapped back to None in mine_with_llm().
                "type": "string",
                "enum": ["qualitative", "quantitative", "unknown"],
                "description": "Whether this is a qualitative (detection) or quantitative (enumeration) validation study. 'unknown' if genuinely undeterminable.",
            },
            "qualitative": {
                "type": ["object", "null"],
                "description": "Populate ONLY if method_nature is 'qualitative'; otherwise null.",
                "properties": {
                    "method_comparison_by_category": {"type": "array", "items": _CATEGORY_ROW_QUALITATIVE},
                },
                "required": ["method_comparison_by_category"],
                "additionalProperties": False,
            },
            "quantitative": {
                "type": ["object", "null"],
                "description": "Populate ONLY if method_nature is 'quantitative'; otherwise null.",
                "properties": {
                    "relative_trueness_by_category": {"type": "array", "items": _CATEGORY_ROW_QUANTITATIVE},
                    "acceptability_limit_log": {
                        "type": ["number", "null"],
                        "description": "The stated Acceptability Limit, in log10 units (e.g. from 'Acceptability Limit fixed at +/- 0.5 log').",
                    },
                },
                "required": ["relative_trueness_by_category", "acceptability_limit_log"],
                "additionalProperties": False,
            },
            "extraction_notes": {
                "type": ["string", "null"],
                "description": "Anything genuinely ambiguous or low-confidence about this extraction -- e.g. a table you weren't fully sure was the right one, or illegible values. Null if nothing to flag.",
            },
        },
        "required": ["certificate_number", "method_nature", "qualitative", "quantitative", "extraction_notes"],
        "additionalProperties": False,
    },
}

PROMPT = """This PDF is a validation study summary report for a microbiology detection/enumeration \
method, produced under the EN ISO 16140-2:2016 protocol. Find:

1. The certificate number on the cover page.
2. Whether this is a qualitative (detection) or quantitative (enumeration) method.
3. The per-food-category results table -- for qualitative studies, the "method comparison" table \
(sensitivity of the alternative/reference methods, relative trueness, false positive ratio per \
category); for quantitative studies, the "relative trueness" table (bias, standard deviation, 95% \
confidence interval per category), plus the stated Acceptability Limit.

Different labs format this table very differently -- some split a category's data across a \
"Total" row below it, some render special symbols as garbled characters. Read the actual page \
content (not just visible characters) to recover the correct numeric values regardless of layout.

Record ONLY real food categories -- one entry per actual ISO 16140-2 food category tested. These \
tables usually also carry summary rows aggregating across categories ("All categories", "Total", \
"All categories - Specific protocol 1", ...): those are totals, not categories, so leave them out \
entirely. If a report splits its results by protocol or variant, record the categories of the \
primary/canonical study only, and describe the other variants in extraction_notes.

This is a single, final call to record_performance_data -- there is no follow-up turn where you \
can correct or replace it. Never submit a draft, placeholder, or test value (e.g. literally \
writing "placeholder" or "test" in a field) to fill the call before you have actually read the \
document; do the real extraction first, then make one call with your genuine findings. If a field \
is genuinely not present or not confidently readable after reading the actual document, use null \
for it and explain why in extraction_notes -- that is the correct way to express uncertainty, not \
a placeholder value."""


# Rows aggregating ACROSS categories rather than describing one -- "All
# categories", "Total", and the per-protocol variants real reports use
# ("All categories - Specific protocol 1", "Total Rapid Spin protocol",
# "All categories (Listeria monocytogenes)"). Confirmed against a real
# 25-record backfill batch, where 18/20 successfully-mined records carried
# at least one: left in, they surface as fake food categories in the
# frontend's category axis and skew any cross-method comparison, which is
# the whole point of the tool. summary_report_parser.py's deterministic
# path already skipped these; the LLM path needs the same guard.
#
# Deliberately anchored at the start of the label and requiring a real
# aggregate word, so a genuine category is never dropped: ISO 16140-2
# Annex A's category names ("Meat products", "Dairy products", ...) don't
# begin with "total"/"overall", nor with "all" followed by
# categories/products/matrices/protocols.
_AGGREGATE_CATEGORY_RE = re.compile(
    r'^\s*(?:all\s+(?:categor|product|matri|protocol)|total\b|overall\b|global\b)', re.I,
)


def _drop_aggregate_rows(rows: list, cert_label: str) -> list:
    """Remove cross-category summary rows, logging each drop so the choice
    stays auditable in the run log rather than silently reshaping data."""
    kept = []
    for row in rows or []:
        name = (row.get("category") or "").strip()
        if _AGGREGATE_CATEGORY_RE.match(name):
            print(f"[{cert_label}] dropping aggregate row {name!r} (not a food category)", file=sys.stderr)
            continue
        kept.append(row)
    return kept


# --- Page selection -------------------------------------------------------
#
# A native-PDF request is billed for each page's text AND a rendered image of
# that page -- roughly 2,000-2,500 tokens per page. These reports run to
# 96-102 pages, so sending the whole document cost about $0.40 a report and
# is the single reason the first backfill batch spent ~$25 without clearing
# the backlog. The answer lives on three or four pages.
#
# Selection is free (pypdf text extraction) and deliberately conservative:
# the model still sees whole pages, so nothing about the extraction task
# changes -- only how many pages it is handed.

MAX_PAGES_SENT = 14

# Scored, not grepped. diagnose_llm_failures.py's flat keyword regex was
# written to answer "where might the table be?" for a human reading a log,
# and "categor" alone matches most of a report -- fine for that job, useless
# as a page filter. These are separate signal classes so a page has to show
# more than one KIND of evidence to score well.
_PAGE_SIGNALS = [
    re.compile(r'relative trueness|justesse relative', re.I),
    re.compile(r'accuracy profile|profil d.exactitude', re.I),
    re.compile(r'acceptability limit|limite d.acceptabilit', re.I),
    re.compile(r'sensitivity|specificity|false positive|sensibilit', re.I),
    re.compile(r'\bbias\b|\bbiais\b|standard deviation|ecart[- ]type', re.I),
    re.compile(r'method comparison|comparaison des m[ée]thodes', re.I),
    re.compile(r'\bLOD\b|\bRLOD\b|detection limit', re.I),
]

# A results table is mostly numbers. This separates the table itself from the
# prose paragraph that introduces it three pages earlier.
_NUMERIC_RE = re.compile(r'-?\d+[.,]\d+')


def _score_page(text: str) -> int:
    if not text:
        return 0
    signals = sum(bool(rx.search(text)) for rx in _PAGE_SIGNALS)
    if not signals:
        return 0
    numbers = len(_NUMERIC_RE.findall(text))
    # Decimal numbers are the strongest single tell, but capped so one dense
    # statistical annex cannot outrank every real table in the document.
    return signals * 2 + min(numbers // 5, 6)


def select_pages(reader: "pypdf.PdfReader", max_pages: int = MAX_PAGES_SENT) -> list:
    """Page indices worth sending, always including the cover.

    Page 0 is unconditional: the prompt's first question is the certificate
    number, which is printed on the cover and nowhere near the tables.

    Each selected page pulls in the one after it. Real tables in these
    reports routinely break across a page boundary, and the continuation
    page is often just numeric rows with no heading -- it scores near zero on
    its own while carrying half the categories.

    Returns every page when the document is already small enough, and when
    nothing scores at all: a report we cannot locate tables in is exactly the
    one where guessing a subset would turn a hard extraction into an
    impossible one.
    """
    n = len(reader.pages)
    if n <= max_pages:
        return list(range(n))

    scored = []
    for i, page in enumerate(reader.pages):
        try:
            score = _score_page(page.extract_text() or "")
        except Exception:  # noqa: BLE001 -- one unparseable page must not lose the report
            score = 0
        if score:
            scored.append((score, i))

    if not scored:
        return list(range(n))

    keep = {0}
    for _, i in sorted(scored, reverse=True):
        if len(keep | {i, i + 1}) > max_pages:
            continue
        keep.add(i)
        if i + 1 < n:
            keep.add(i + 1)
    return sorted(keep)


def _format_pages(pages: list) -> str:
    """1-indexed, collapsed to ranges -- "1, 44-47" reads; a list of 14
    indices in a log line does not."""
    if not pages:
        return "none"
    out, start, prev = [], pages[0], pages[0]
    for i in pages[1:] + [None]:
        if i == prev + 1:
            prev = i
            continue
        out.append(str(start + 1) if start == prev else f"{start + 1}-{prev + 1}")
        start = prev = i
    return ", ".join(out)


def _read_pdf_bytes_decrypted(pdf_path: Path, max_pages: int = MAX_PAGES_SENT):
    """Some real NF-Validation reports are permissions-encrypted (confirmed
    directly on a report named "..._B_SR_v0-protected.pdf": AES-256,
    print/copy/change all disabled) -- the same real-password-protection
    NF-Validation already uses elsewhere, which summary_report_parser.py
    already opens by decrypting with an empty password (confirmed to work
    on every sampled AFNOR PDF). llm_report_miner.py was sending the raw,
    still-encrypted file bytes straight to the API -- confirmed as the real
    cause of a placeholder/junk extraction on that report: the file reads
    fine locally once decrypted (Table 4 is a perfectly ordinary table), so
    the model was working from degraded/empty content, not a hard document,
    and improvised a placeholder rather than surfacing a clean error.
    Rewrites the decrypted pages into a fresh in-memory PDF so what's sent
    to the API is always plain, unencrypted bytes.

    Also drops the pages that cannot contain the answer -- see select_pages.
    Returns (pdf_bytes, page_indices, total_pages) so the caller can record
    what was actually sent: when an extraction comes back wrong, "we only
    showed it pages 1, 44-47" is the first thing worth knowing, and without
    it that is unrecoverable after the fact."""
    reader = pypdf.PdfReader(str(pdf_path))
    if reader.is_encrypted:
        reader.decrypt("")

    total = len(reader.pages)
    pages = select_pages(reader, max_pages)

    if not reader.is_encrypted and len(pages) == total:
        # Nothing to strip and nothing to decrypt: send the file untouched
        # rather than round-tripping it through pypdf, which is the one path
        # that could alter bytes the API already handles correctly.
        return pdf_path.read_bytes(), pages, total

    writer = pypdf.PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), pages, total



def make_client() -> anthropic.Anthropic:
    """The API client, with the workspace header when the key needs one.

    An identity-linked API key must name the workspace it acts in or the
    API answers 400 "anthropic-workspace-id is required". This project's
    key is one: the Cloudflare chat proxy hit exactly that and needed the
    same header. Ordinary keys must NOT send it, so it goes out only when
    ANTHROPIC_WORKSPACE_ID is set -- which keeps this working with either
    kind of key, and keeps an account identifier out of the repository.
    """
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        return anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace})
    return anthropic.Anthropic()


def build_request_params(pdf_path: Path, max_pages: int = MAX_PAGES_SENT) -> dict:
    """The body of one mining request, with no API call made.

    Split out from mine_with_llm so the synchronous path and the Batch path
    send byte-identical requests. Having two copies of this dict would mean
    the batch route could drift from the one that was actually calibrated --
    disable_parallel_tool_use below is there because of a specific observed
    failure, and it must not be the kind of detail that survives in only one
    of two code paths.
    """
    pdf_bytes, pages_sent, total_pages = _read_pdf_bytes_decrypted(pdf_path, max_pages)
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    if len(pages_sent) < total_pages:
        print(f"  sending {len(pages_sent)}/{total_pages} pages: "
              f"{_format_pages(pages_sent)}", file=sys.stderr)

    return {
        "model": MODEL,
        "max_tokens": 4096,
        "tools": [RECORD_PERFORMANCE_TOOL],
        # disable_parallel_tool_use: without it, a first real calibration run
        # showed Claude sometimes emits a throwaway first tool_use block
        # (literally {"extraction_notes": "Placeholder call; will correct in
        # next call."}, all other fields empty) before a real, corrected one
        # in the same response -- taking response.content's FIRST tool_use
        # block picked up that placeholder instead of the real extraction.
        "tool_choice": {"type": "tool", "name": "record_performance_data",
                        "disable_parallel_tool_use": True},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }


def parse_response(response, pdf_label: str = "report") -> dict:
    """Turn one API response into the project's mined-record shape.

    Also split out for the Batch path: a batch result carries the same
    Message object as a synchronous call, so everything after the request is
    identical and should not be written twice.
    """
    # Defense in depth alongside disable_parallel_tool_use above: use the
    # LAST tool_use block, not the first, in case a self-correction slips
    # through anyway.
    tool_uses = [b for b in response.content if b.type == "tool_use"]
    if not tool_uses:
        raise RuntimeError(f"No tool_use block in Claude's response (stop_reason={response.stop_reason!r})")
    tool_use = tool_uses[-1]

    result = tool_use.input
    if result["method_nature"] == "unknown":
        result["method_nature"] = None
    # The prompt asks for real categories only, but a deterministic filter
    # runs regardless -- an instruction is not a guarantee, and an aggregate
    # row silently becoming a fake food category is exactly the kind of
    # error that would be hard to spot once it reached the frontend.
    cert_label = result.get("certificate_number") or pdf_label
    performance = None
    if result["method_nature"] == "quantitative" and result["quantitative"]:
        performance = {
            "method_nature": "quantitative",
            "quantitative": {
                "relative_trueness_by_category": _drop_aggregate_rows(
                    result["quantitative"]["relative_trueness_by_category"], cert_label),
                "accuracy_profile": {
                    "acceptability_limit_log": result["quantitative"]["acceptability_limit_log"],
                    "by_matrix": [],
                },
                "loq_log": None,
                "inclusivity": {},
                "exclusivity": {},
            },
        }
    elif result["method_nature"] == "qualitative" and result["qualitative"]:
        performance = {
            "method_nature": "qualitative",
            "qualitative": {
                "method_comparison_by_category": _drop_aggregate_rows(
                    result["qualitative"]["method_comparison_by_category"], cert_label),
                "inclusivity": {},
                "exclusivity": {},
            },
        }

    return {
        "certificate_number": result["certificate_number"],
        "method_nature": result["method_nature"],
        "performance": performance,
        "extraction_notes": result["extraction_notes"],
        "mining_notes": (
            f"Performance data extracted by {MODEL} reading the PDF directly (native document "
            "input), used as a fallback because the deterministic pdfplumber pipeline "
            "(summary_report_parser.py) found no per-category breakdown in this report. "
            + (f"Model's own extraction notes: {result['extraction_notes']}" if result["extraction_notes"] else "")
        ).strip(),
    }


def mine_with_llm(pdf_path: Path, client: anthropic.Anthropic | None = None,
                  max_pages: int = MAX_PAGES_SENT) -> dict:
    """Extract cover metadata + per-category performance data directly from
    the PDF via Claude, bypassing pdfplumber/pypdf entirely. Returns the same
    shape as summary_report_parser.mine_performance()'s relevant fields, so
    callers can treat the two interchangeably.

    One synchronous call. For the whole backlog at once, and at half the
    price, see backfill_llm_performance.py --batch, which sends the same
    request through the Batch API.
    """
    client = client or make_client()
    response = client.messages.create(**build_request_params(pdf_path, max_pages))
    return parse_response(response, pdf_path.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    args = ap.parse_args()

    mined = mine_with_llm(Path(args.pdf))
    print(json.dumps(mined, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
