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

Requires a real Anthropic API key (console.anthropic.com -- separate from a
claude.ai chat subscription, which does not include API access) in the
ANTHROPIC_API_KEY environment variable.

Usage (offline, from a saved summary-report PDF):
    python3 llm_report_miner.py --pdf path/to/report.pdf
"""
import argparse
import base64
import json
import sys
from pathlib import Path

import anthropic

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
                "type": ["string", "null"],
                "enum": ["qualitative", "quantitative", None],
                "description": "Whether this is a qualitative (detection) or quantitative (enumeration) validation study.",
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
content (not just visible characters) to recover the correct numeric values regardless of layout. \
Call record_performance_data with what you find; use null for anything genuinely not present or \
not confidently readable, and use extraction_notes to flag anything you're unsure about."""


def mine_with_llm(pdf_path: Path, client: anthropic.Anthropic | None = None) -> dict:
    """Extract cover metadata + per-category performance data directly from
    the PDF via Claude, bypassing pdfplumber/pypdf entirely. Returns the same
    shape as summary_report_parser.mine_performance()'s relevant fields, so
    callers can treat the two interchangeably."""
    client = client or anthropic.Anthropic()
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("ascii")

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[RECORD_PERFORMANCE_TOOL],
        tool_choice={"type": "tool", "name": "record_performance_data"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(f"No tool_use block in Claude's response (stop_reason={response.stop_reason!r})")

    result = tool_use.input
    performance = None
    if result["method_nature"] == "quantitative" and result["quantitative"]:
        performance = {
            "method_nature": "quantitative",
            "quantitative": {
                "relative_trueness_by_category": result["quantitative"]["relative_trueness_by_category"],
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
                "method_comparison_by_category": result["qualitative"]["method_comparison_by_category"],
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
