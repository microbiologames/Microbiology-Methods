"""Backfill performance data into data/methods/ records the deterministic
pdfplumber pipeline couldn't mine, using scrapers/llm_report_miner.py.

Targets exactly the records that have a summary_report_pdf_url but NO real
per-category breakdown (91 of them as of writing; a further 5 have no
report URL at all and are unreachable by any miner). Records the
deterministic pipeline already mined are never touched -- that data is
free, deterministic and auditable, and there's no reason to pay to
re-derive it or risk replacing it with a less certain extraction.

Every record written here is marked traceability.extraction_confidence =
"medium" (vs. "high" for the deterministic path) so LLM-extracted numbers
stay distinguishable in the data forever, and the model's own
extraction_notes are appended to traceability.notes -- on the real
calibration sample those notes surfaced genuine source-document
inconsistencies (two conflicting bias columns; per-category n disagreeing
between tables), so they're worth keeping rather than discarding.

Calibration status before this was trusted (see validate_llm_miner.py and
.github/workflows/calibrate_llm_miner.yml): on 5 known-good reports, the
LLM path reproduced the deterministic pipeline's numbers exactly --
every compared field within +/-0.05.

Cost control: --limit caps how many records are processed in one run
(each is one paid API call), and --skip-existing means an interrupted or
partial run can simply be re-run to continue where it left off.

Usage:
    python3 backfill_llm_performance.py --methods-dir ../data/methods --limit 25
"""
import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

import jsonschema

from llm_report_miner import mine_with_llm


def has_real_breakdown(record: dict) -> bool:
    perf = record.get("performance")
    if not perf:
        return False
    nature = perf.get("method_nature")
    rows = (perf.get("quantitative", {}).get("relative_trueness_by_category")
            if nature == "quantitative" else perf.get("qualitative", {}).get("method_comparison_by_category"))
    return bool(rows)


def find_targets(methods_dir: Path, skip_existing: bool = True):
    """(path, record, pdf_url) for every NF-Validation record that has a
    summary report to mine but no real per-category breakdown yet."""
    targets = []
    for f in sorted(methods_dir.glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        if record.get("source") == "MICROVAL":
            # MicroVal reports aren't wired into this miner yet -- its
            # summary_report_pdf_url points at a differently-structured
            # document this prompt hasn't been calibrated against.
            continue
        if skip_existing and has_real_breakdown(record):
            continue
        pdf_url = record.get("traceability", {}).get("summary_report_pdf_url")
        if not pdf_url:
            continue
        targets.append((f, record, pdf_url))
    return targets


def merge_mined(record: dict, mined: dict) -> dict:
    record["performance"] = mined["performance"]
    tr = record.setdefault("traceability", {})
    # "medium", never "high": this is a real, calibrated extraction but it
    # is not the deterministic path, and that distinction should survive in
    # the data rather than living only in a commit message.
    tr["extraction_confidence"] = "medium"
    notes = (tr.get("notes") or "").strip()
    addition = mined["mining_notes"]
    if addition and addition not in notes:
        tr["notes"] = f"{notes} {addition}".strip()
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="../data/methods")
    ap.add_argument("--schema", default="../schema/method.schema.json")
    ap.add_argument("--limit", type=int, default=25,
                    help="Max records to process this run -- each is one paid API call.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be processed and exit, without any API call.")
    args = ap.parse_args()

    methods_dir = Path(args.methods_dir)
    targets = find_targets(methods_dir)
    print(f"{len(targets)} record(s) still need a performance backfill; "
          f"processing up to {args.limit} this run.", file=sys.stderr)

    if args.dry_run:
        for path, record, pdf_url in targets[:args.limit]:
            print(f"  would mine {record['source_certificate_number']}: {pdf_url}", file=sys.stderr)
        return

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    written = failed = no_data = invalid = 0
    for path, record, pdf_url in targets[:args.limit]:
        cert = record["source_certificate_number"]
        print(f"[{cert}] mining {pdf_url}", file=sys.stderr)
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                urllib.request.urlretrieve(pdf_url, tmp.name)
                mined = mine_with_llm(Path(tmp.name))
        except Exception as exc:  # noqa: BLE001 -- one bad report must not abort the batch
            print(f"[{cert}] ERROR: {exc}", file=sys.stderr)
            failed += 1
            continue

        if not mined["performance"] or not has_real_breakdown({"performance": mined["performance"]}):
            # Confirmed real failure mode during calibration: a response
            # that parses fine but carries an empty category array. Left
            # untouched (not written as an empty result) so a later run can
            # retry it rather than it looking permanently mined-but-empty.
            print(f"[{cert}] no usable breakdown returned; leaving record untouched "
                  f"(notes={mined.get('extraction_notes')!r})", file=sys.stderr)
            no_data += 1
            continue

        candidate = merge_mined(json.loads(json.dumps(record)), mined)
        errors = list(validator.iter_errors(candidate))
        if errors:
            print(f"[{cert}] SCHEMA ERROR, not written: {errors[0].message}", file=sys.stderr)
            invalid += 1
            continue

        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = (candidate["performance"].get("quantitative", {}).get("relative_trueness_by_category")
                or candidate["performance"].get("qualitative", {}).get("method_comparison_by_category"))
        print(f"[{cert}] wrote {len(rows)} category row(s)", file=sys.stderr)
        written += 1

    remaining = len(targets) - written
    print(f"\n=== Backfill: {written} written, {no_data} returned no usable data, "
          f"{failed} errored, {invalid} schema-invalid; {remaining} record(s) still "
          f"need backfilling after this run ===", file=sys.stderr)


if __name__ == "__main__":
    main()
