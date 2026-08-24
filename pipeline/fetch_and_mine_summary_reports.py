"""Download each data/methods/ record's summary-report PDF (NF-Validation
only, for now) and mine performance data from it via
scrapers/summary_report_parser.py.

Intended to run in a normal-egress environment (the GitHub Actions
workflow) -- this codebase's own development environment has outbound
HTTPS blocked to nf-validation.afnor.org, so this has only been exercised
against the one manually-supplied report (see summary_report_parser.py's
own module docstring for what its extraction does and doesn't cover yet).

Downloaded PDFs are kept in a temp directory and discarded, not committed --
per the project's policy of not vendoring third-party copyrighted source
documents, only the extracted factual data.
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scrapers"))
from summary_report_parser import mine_performance, merge_into_method_record  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="data/methods")
    ap.add_argument("--schema", default="schema/method.schema.json")
    ap.add_argument("--delay-seconds", type=float, default=2.0)
    ap.add_argument("--skip-already-mined", action="store_true",
                     help="Skip records that already have a non-null performance field.")
    args = ap.parse_args()

    methods_dir = Path(args.methods_dir)
    schema_path = Path(args.schema)

    fetched = merged = skipped = failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for record_path in sorted(methods_dir.glob("*.json")):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            url = record.get("traceability", {}).get("summary_report_pdf_url")
            if not url:
                continue
            if args.skip_already_mined and record.get("performance") is not None:
                skipped += 1
                continue

            try:
                resp = requests.get(url, timeout=60, headers={"User-Agent": "microbiology-methods-bot/1.0"})
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(f"FETCH ERROR [{url}]: {exc}", file=sys.stderr)
                failed += 1
                continue
            fetched += 1

            pdf_path = tmp_path / f"{record_path.stem}.pdf"
            pdf_path.write_bytes(resp.content)

            mined = mine_performance(pdf_path)
            if merge_into_method_record(mined, methods_dir, schema_path):
                merged += 1
            else:
                failed += 1

            time.sleep(args.delay_seconds)

    print(
        f"Fetched {fetched} summary reports, merged {merged}, "
        f"skipped {skipped}, failed {failed}.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
