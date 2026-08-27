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


def _read_pdf_bytes_decrypted(pdf_path: Path) -> bytes:
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
    to the API is always plain, unencrypted bytes -- a no-op (same bytes
    back out) for the majority of reports that were never encrypted."""
    reader = pypdf.PdfReader(str(pdf_path))
    if not reader.is_encrypted:
        return pdf_path.read_bytes()
    reader.decrypt("")
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def mine_with_llm(pdf_path: Path, client: anthropic.Anthropic | None = None) -> dict:
    """Extract cover metadata + per-category performance data directly from
    the PDF via Claude, bypassing pdfplumber/pypdf entirely. Returns the same
    shape as summary_report_parser.mine_performance()'s relevant fields, so
    callers can treat the two interchangeably."""
    client = client or anthropic.Anthropic()
    pdf_b64 = base64.standard_b64encode(_read_pdf_bytes_decrypted(pdf_path)).decode("ascii")

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[RECORD_PERFORMANCE_TOOL],
        # disable_parallel_tool_use: without it, a first real calibration run
        # showed Claude sometimes emits a throwaway first tool_use block
        # (literally {"extraction_notes": "Placeholder call; will correct in
        # next call."}, all other fields empty) before a real, corrected one
        # in the same response -- taking response.content's FIRST tool_use
        # block picked up that placeholder instead of the real extraction.
        tool_choice={"type": "tool", "name": "record_performance_data", "disable_parallel_tool_use": True},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )

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
    cert_label = result.get("certificate_number") or pdf_path.name
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    args = ap.parse_args()

    mined = mine_with_llm(Path(args.pdf))
    print(json.dumps(mined, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
