"""One-off debug helper: download a handful of summary-report PDFs and dump
their raw pypdf-extracted text, so a human (or the next mining-parser
iteration) can see real report wording without guessing at it.

Only meant to be run ad hoc from a normal-egress environment (e.g. GitHub
Actions) while extending scrapers/summary_report_parser.py's category
extraction to more report shapes -- not part of the regular pipeline.

Usage:
    python3 debug_dump_report_text.py --out-dir debug/reports \
        "https://.../report1.pdf" "https://.../report2.pdf"
"""
import argparse
import sys
import tempfile
from pathlib import Path

import pypdf
import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--out-dir", default="debug/reports")
    ap.add_argument("--max-chars", type=int, default=12000)
    ap.add_argument("--skip-chars", type=int, default=0,
                     help="Skip this many leading characters (e.g. to jump past a report's boilerplate front matter/TOC).")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, url in enumerate(args.urls):
            print(f"[{i}] fetching {url}", file=sys.stderr)
            try:
                resp = requests.get(url, timeout=60, headers={"User-Agent": "microbiology-methods-bot/1.0"})
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(f"[{i}] FETCH ERROR: {exc}", file=sys.stderr)
                continue

            pdf_path = tmp_path / f"report_{i}.pdf"
            pdf_path.write_bytes(resp.content)

            try:
                reader = pypdf.PdfReader(str(pdf_path))
                if reader.is_encrypted:
                    reader.decrypt("")
                full_text = "\n".join(p.extract_text() or "" for p in reader.pages)
            except Exception as exc:  # noqa: BLE001 -- diagnostic only
                print(f"[{i}] PARSE ERROR: {exc}", file=sys.stderr)
                continue

            # Some reports carry a huge raw-sample-data appendix (thousands
            # of individual result rows) that swamped the job log on the
            # first run of this script without showing anything useful --
            # the actual summary tables this exists to inspect are earlier
            # in the document, so cap what gets written/printed. --skip-chars
            # additionally jumps past a report's own boilerplate front
            # matter/table-of-contents when that's known to run long (the
            # real tables of interest start well into the document).
            windowed = full_text[args.skip_chars:args.skip_chars + args.max_chars]
            out_path = out_dir / f"report_{i}.txt"
            out_path.write_text(windowed, encoding="utf-8")
            print(f"[{i}] wrote chars [{args.skip_chars}:{args.skip_chars + len(windowed)}] "
                  f"of {len(full_text)} total -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
