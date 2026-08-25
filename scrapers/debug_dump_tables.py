"""One-off debug helper: download a summary-report PDF and dump pdfplumber's
structural table extraction (real cell grid, not linear text) for a given
page range, so the next iteration of scrapers/summary_report_parser.py's
category-table extraction can be built against real cell boundaries instead
of guessed at from linear text.

Only meant to be run ad hoc from a normal-egress environment (e.g. GitHub
Actions) -- nf-validation.afnor.org is proxy-blocked from this repo's own
dev environment.

Usage:
    python3 debug_dump_tables.py --first-page 13 --last-page 15 \
        "https://.../report.pdf"
"""
import argparse
import pprint
import sys
import tempfile
from pathlib import Path

import pdfplumber
import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--first-page", type=int, required=True, help="1-indexed, inclusive.")
    ap.add_argument("--last-page", type=int, required=True, help="1-indexed, inclusive.")
    args = ap.parse_args()

    print(f"fetching {args.url}", file=sys.stderr)
    resp = requests.get(args.url, timeout=60, headers={"User-Agent": "microbiology-methods-bot/1.0"})
    resp.raise_for_status()

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "report.pdf"
        pdf_path.write_bytes(resp.content)

        with pdfplumber.open(pdf_path) as pdf:
            print(f"total pages: {len(pdf.pages)}", file=sys.stderr)
            for page_num in range(args.first_page, args.last_page + 1):
                if page_num < 1 or page_num > len(pdf.pages):
                    continue
                page = pdf.pages[page_num - 1]
                tables = page.extract_tables()
                print(f"\n=== page {page_num}: {len(tables)} table(s) found ===")
                for t_idx, table in enumerate(tables):
                    print(f"--- table {t_idx} ({len(table)} rows) ---")
                    pprint.pprint(table, width=140)


if __name__ == "__main__":
    main()
