"""Aggregate data/methods/*.json into web/data.json for the static frontend.

Scope: ISO 16140-2 validated methods only (NF-Validation, MicroVal) for now.
AOAC-RI records are excluded -- not because they're bad data, but because
AOAC-RI's own live listing is confirmed broken on AOAC's side (see
scrapers/aoac_ptm_live_fetch.py's module docstring), so the only AOAC-RI
records that exist are 4 leftover from the project's initial manual
bootstrap, not a real, refreshable slice of that source. Presenting 4
static AOAC-RI records next to the two live-refreshed ISO 16140 sources
would misrepresent the tool's actual coverage. Revisit this exclusion once
AOAC-RI scraping is picked back up.

Two axes are computed for the heatmap, and they come from deliberately
different fields -- conflating them would misrepresent what a certification
actually means:

  - method_category: the detection technology (culture media / molecular
    PCR / immunological / ...), from method_type.category. Well-populated
    across every source.

  - tested_categories: the food categories actually exercised during the
    validation STUDY, normalized onto ISO 16140-2:2016 Annex A's own fixed
    18-category taxonomy (see food_categories.py) rather than left as
    whatever free-text label a given report happened to use -- mined
    reports turned out to use ~108 distinct raw strings for what's really
    at most 18 real categories ("Dairy products" / "Milk & Dairy products"
    / "Raw dairy products" / "Raw milk and dairy products" / ... are all
    the same category), which made the frontend's food-category axis
    useless as a fixed, comparable set of columns. For ISO 16140-2
    validations (NF-Validation, MicroVal), the certificate's own
    validation_scope is essentially never a useful "matrix" signal: per
    the project owner, once a method has been validated across >=5 food
    categories its official scope becomes "BRF" (Broad Range of Food)
    regardless of which ones -- so validation_scope text collapses to "all
    food products" for the overwhelming majority of NF-Validation methods
    (verified: 137/142 records, and 95%+ of those are a "TOUS PRODUITS
    D'ALIMENTATION HUMAINE" variant). What's actually informative is which
    specific categories were tested to reach BRF, which lives in the mined
    performance data
    (performance.qualitative.method_comparison_by_category[].category or
    performance.quantitative.relative_trueness_by_category[].category) --
    not in validation_scope at all. AOAC-RI has no BRF concept and lists
    its actually-narrower tested matrices directly in validation_scope, so
    that's used as the fallback when no performance breakdown is mined yet
    (also run through the same Annex A normalization).

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from food_categories import ANNEX_A_CATEGORIES, LABEL_BY_ID, normalize_food_category  # noqa: E402


def normalize_categories(raw_categories: list, unclassified_log: list) -> list:
    """Map raw free-text category labels to Annex A labels, deduped and
    ordered per Annex A's own category sequence (not alphabetically, and
    not by first appearance) so every method's tested_categories list
    sorts consistently. A raw label matching no known food family is
    appended to unclassified_log verbatim rather than dropped, so the
    caller can report exactly what's still unclassified."""
    order = {c["id"]: i for i, c in enumerate(ANNEX_A_CATEGORIES)}
    ids = set()
    for raw in raw_categories:
        cid = normalize_food_category(raw)
        if cid is None:
            unclassified_log.append(raw)
        else:
            ids.add(cid)
    return [LABEL_BY_ID[cid] for cid in sorted(ids, key=lambda i: order[i])]


def extract_tested_categories(record: dict, unclassified_log: list) -> list:
    performance = record.get("performance")
    raw_categories = []
    if performance:
        nature = performance.get("method_nature")
        if nature == "qualitative":
            entries = performance.get("qualitative", {}).get("method_comparison_by_category", [])
        elif nature == "quantitative":
            entries = performance.get("quantitative", {}).get("relative_trueness_by_category", [])
        else:
            entries = []
        raw_categories = [e["category"] for e in entries if e.get("category")]

    if not raw_categories:
        # Fall back to the certificate's own validation_scope.matrices --
        # the right source for AOAC-RI (no BRF concept, lists real tested
        # matrices directly), and for the small minority of NF-Validation
        # records whose scope hasn't collapsed to "all food products".
        raw_categories = list(record.get("validation_scope", {}).get("matrices") or [])

    return normalize_categories(raw_categories, unclassified_log)


def build_entry(record: dict, unclassified_log: list) -> dict:
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
        "tested_categories": extract_tested_categories(record, unclassified_log),
        "has_performance_data": bool(record.get("performance")),
        "record": record,
    }


EXCLUDED_SOURCES = {"AOAC-RI"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="data/methods")
    ap.add_argument("--out", default="web/data.json")
    args = ap.parse_args()

    entries = []
    skipped = 0
    unclassified_log = []
    for f in sorted(Path(args.methods_dir).glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        if record.get("source") in EXCLUDED_SOURCES:
            skipped += 1
            continue
        entries.append(build_entry(record, unclassified_log))
    if skipped:
        print(f"Skipped {skipped} record(s) from excluded source(s) {sorted(EXCLUDED_SOURCES)}")
    if unclassified_log:
        from collections import Counter
        counts = Counter(unclassified_log)
        print(f"{len(unclassified_log)} raw category mention(s) matched no Annex A food "
              f"family ({len(counts)} distinct) -- left out of tested_categories rather "
              f"than guessed:", file=sys.stderr)
        for raw, n in counts.most_common():
            print(f"  {n:3d}x {raw!r}", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_from": args.methods_dir,
                "count": len(entries),
                "food_categories": [c["en"] for c in ANNEX_A_CATEGORIES],
                "methods": entries,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(entries)} methods -> {out_path}")


if __name__ == "__main__":
    main()
