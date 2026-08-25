"""Aggregate data/methods/*.json into web/data.json for the static frontend.

Two axes are computed for the heatmap, and they come from deliberately
different fields -- conflating them would misrepresent what a certification
actually means:

  - method_category: the detection technology (culture media / molecular
    PCR / immunological / ...), from method_type.category. Well-populated
    across every source.

  - tested_categories: the food categories actually exercised during the
    validation STUDY. For ISO 16140-2 validations (NF-Validation, MicroVal),
    the certificate's own validation_scope is essentially never a useful
    "matrix" signal: per the project owner, once a method has been
    validated across >=5 food categories its official scope becomes "BRF"
    (Broad Range of Food) regardless of which ones -- so validation_scope
    text collapses to "all food products" for the overwhelming majority of
    NF-Validation methods (verified: 137/142 records, and 95%+ of those are
    a "TOUS PRODUITS D'ALIMENTATION HUMAINE" variant). What's actually
    informative is which specific categories were tested to reach BRF,
    which lives in the mined performance data
    (performance.qualitative.method_comparison_by_category[].category or
    performance.quantitative.relative_trueness_by_category[].category) --
    not in validation_scope at all. AOAC-RI has no BRF concept and lists
    its actually-narrower tested matrices directly in validation_scope, so
    that's used as the fallback when no performance breakdown is mined yet.

  This means tested_categories is only as complete as the summary-report
  mining is: today (before that mining has run broadly) most NF-Validation
  methods will show zero tested_categories, populated incrementally as
  pipeline/fetch_and_mine_summary_reports.py mines more reports over time.
  This is by design, not a bug to silently paper over -- has_performance_data
  tells the frontend (and the reader) which is which.

Usage:
    python3 build_frontend_data.py --methods-dir data/methods --out web/data.json
"""
import argparse
import json
from pathlib import Path


def extract_tested_categories(record: dict) -> list:
    performance = record.get("performance")
    if performance:
        nature = performance.get("method_nature")
        if nature == "qualitative":
            entries = performance.get("qualitative", {}).get("method_comparison_by_category", [])
        elif nature == "quantitative":
            entries = performance.get("quantitative", {}).get("relative_trueness_by_category", [])
        else:
            entries = []
        categories = [e["category"] for e in entries if e.get("category")]
        if categories:
            seen = set()
            return [c for c in categories if not (c in seen or seen.add(c))]

    # Fall back to the certificate's own validation_scope.matrices -- the
    # right source for AOAC-RI (no BRF concept, lists real tested matrices
    # directly), a placeholder for NF-Validation until its reports are mined.
    return list(record.get("validation_scope", {}).get("matrices") or [])


def build_entry(record: dict) -> dict:
    return {
        "id": record["id"],
        "source": record["source"],
        "source_certificate_number": record["source_certificate_number"],
        "commercial_name": record["commercial_name"],
        "manufacturer_name": (record.get("manufacturer") or {}).get("name"),
        "organism": (record.get("target_organism") or {}).get("normalized") or "Unknown",
        "method_category": (record.get("method_type") or {}).get("category") or "other",
        "action": (record.get("method_type") or {}).get("action"),
        "status": (record.get("certification") or {}).get("status", "unknown"),
        "current_expiry": (record.get("certification") or {}).get("current_expiry"),
        "tested_categories": extract_tested_categories(record),
        "has_performance_data": bool(record.get("performance")),
        "record": record,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="data/methods")
    ap.add_argument("--out", default="web/data.json")
    args = ap.parse_args()

    entries = []
    for f in sorted(Path(args.methods_dir).glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        entries.append(build_entry(record))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_from": args.methods_dir,
                "count": len(entries),
                "methods": entries,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(entries)} methods -> {out_path}")


if __name__ == "__main__":
    main()
