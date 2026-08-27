"""Calibrate llm_report_miner.py against known-good ground truth before
trusting it on reports the deterministic pipeline couldn't mine.

Rather than asking a human to review LLM-extracted values by eye, this
compares Claude's extraction against the SAME reports summary_report_parser.py
(pdfplumber, deterministic) already mined correctly -- the 50 NF-Validation
records that currently have a real, non-empty category breakdown. If the LLM
path reproduces those known-correct numbers within a small numeric tolerance,
that's real evidence it can be trusted on the reports where the deterministic
path found nothing at all -- no manual spot-checking required to reach that
conclusion.

This only downloads/reads PDFs already referenced by traceability.
summary_report_pdf_url on existing data/methods/ records; run from an
environment with real egress to nf-validation.afnor.org (this project's own
dev sandbox is proxy-blocked from it -- see the GitHub Actions workflow
pattern used throughout this repo for every other AFNOR/MicroVal fetch).

Usage:
    python3 validate_llm_miner.py --methods-dir data/methods --sample-size 10
"""
import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

from llm_report_miner import mine_with_llm

TOLERANCE = 0.05  # absolute; these are percentages/log-values with 1-2 sig figs in source reports


def _num_close(a, b, tol=TOLERANCE) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def _rows_by_category(rows: list) -> dict:
    return {r["category"].strip().lower(): r for r in rows}


def compare_breakdown(ground_truth: list, llm_rows: list, numeric_fields: list) -> dict:
    """Returns {category: {field: (ground_truth_value, llm_value, match_bool)}}
    for every category present in ground_truth, plus a summary count."""
    gt_by_cat = _rows_by_category(ground_truth)
    llm_by_cat = _rows_by_category(llm_rows)

    report = {}
    total_fields = matched_fields = 0
    missing_categories = []
    for cat, gt_row in gt_by_cat.items():
        llm_row = llm_by_cat.get(cat)
        if llm_row is None:
            missing_categories.append(cat)
            continue
        field_report = {}
        for field in numeric_fields:
            gt_val, llm_val = gt_row.get(field), llm_row.get(field)
            match = _num_close(gt_val, llm_val)
            field_report[field] = (gt_val, llm_val, match)
            total_fields += 1
            matched_fields += int(match)
        report[cat] = field_report

    return {
        "per_category": report,
        "missing_categories": missing_categories,
        "total_fields": total_fields,
        "matched_fields": matched_fields,
    }


def validate_one(record: dict, pdf_path: Path) -> dict:
    nature = record["performance"]["method_nature"]
    if nature == "quantitative":
        gt_rows = record["performance"]["quantitative"]["relative_trueness_by_category"]
        numeric_fields = ["bias_log", "sd_log", "lower_limit_95", "upper_limit_95"]
    else:
        gt_rows = record["performance"]["qualitative"]["method_comparison_by_category"]
        numeric_fields = ["sensitivity_alternative_pct", "sensitivity_reference_pct", "relative_trueness_pct"]

    mined = mine_with_llm(pdf_path)
    if mined["method_nature"] != nature:
        return {"certificate": record["source_certificate_number"], "error":
                f"LLM said method_nature={mined['method_nature']!r}, ground truth is {nature!r}"}
    if not mined["performance"]:
        return {"certificate": record["source_certificate_number"], "error": "LLM returned no performance data at all"}

    llm_rows = (mined["performance"]["quantitative"]["relative_trueness_by_category"] if nature == "quantitative"
                else mined["performance"]["qualitative"]["method_comparison_by_category"])
    comparison = compare_breakdown(gt_rows, llm_rows, numeric_fields)
    comparison["certificate"] = record["source_certificate_number"]
    comparison["llm_extraction_notes"] = mined["extraction_notes"]
    return comparison


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="data/methods")
    ap.add_argument("--sample-size", type=int, default=10,
                     help="How many known-good records to spot-check (cost control -- each is one paid API call).")
    args = ap.parse_args()

    candidates = []
    for f in sorted(Path(args.methods_dir).glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        perf = record.get("performance")
        if not perf:
            continue
        nature = perf.get("method_nature")
        rows = (perf.get("quantitative", {}).get("relative_trueness_by_category")
                if nature == "quantitative" else perf.get("qualitative", {}).get("method_comparison_by_category"))
        url = record.get("traceability", {}).get("source_document_url")
        pdf_url = record.get("traceability", {}).get("summary_report_pdf_url") or url
        if rows and pdf_url:
            candidates.append((record, pdf_url))

    print(f"Found {len(candidates)} known-good records with a real category breakdown "
          f"and a source PDF URL; sampling {min(args.sample_size, len(candidates))}.", file=sys.stderr)

    results = []
    for record, pdf_url in candidates[:args.sample_size]:
        cert = record["source_certificate_number"]
        print(f"[{cert}] downloading {pdf_url}", file=sys.stderr)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            urllib.request.urlretrieve(pdf_url, tmp.name)
            try:
                result = validate_one(record, Path(tmp.name))
            except Exception as exc:  # noqa: BLE001 -- one bad PDF must not abort the whole calibration run
                result = {"certificate": cert, "error": str(exc)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)

    total_fields = sum(r.get("total_fields", 0) for r in results)
    matched_fields = sum(r.get("matched_fields", 0) for r in results)
    errored = [r for r in results if "error" in r]
    print(f"\n=== Calibration summary: {matched_fields}/{total_fields} fields matched "
          f"within +/-{TOLERANCE}, {len(errored)}/{len(results)} records errored ===", file=sys.stderr)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
