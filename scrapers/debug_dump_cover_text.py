"""One-off debug helper: download a summary-report PDF and dump pypdf's
extracted text for its first ~3000 characters (the cover page window
extract_cover_metadata() actually looks at), plus whether pypdf needed
decryption and succeeded, so real failures in certificate-number/method-
nature extraction can be diagnosed from actual wording instead of guessed
at.

Only meant to be run ad hoc from a normal-egress environment (e.g. GitHub
Actions) -- nf-validation.afnor.org is proxy-blocked from this repo's own
dev environment.

Usage:
    python3 debug_dump_cover_text.py "https://.../report.pdf" ["https://.../report2.pdf" ...]
"""
import sys
import tempfile
from pathlib import Path

import pypdf
import requests


def main():
    urls = sys.argv[1:]
    if not urls:
        print("usage: debug_dump_cover_text.py <url> [<url> ...]", file=sys.stderr)
        sys.exit(1)

    for url in urls:
        print(f"\n{'=' * 100}\n{url}\n{'=' * 100}")
        try:
            resp = requests.get(url, timeout=60, headers={"User-Agent": "microbiology-methods-bot/1.0"})
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"FETCH ERROR: {exc}")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "report.pdf"
            pdf_path.write_bytes(resp.content)

            try:
                reader = pypdf.PdfReader(str(pdf_path))
                print(f"is_encrypted: {reader.is_encrypted}")
                if reader.is_encrypted:
                    result = reader.decrypt("")
                    print(f"decrypt(''): {result}")
                text = "\n".join(p.extract_text() or "" for p in reader.pages[:2])
            except Exception as exc:  # noqa: BLE001 -- want to see every failure mode
                print(f"PARSE ERROR: {type(exc).__name__}: {exc}")
                continue

            print(f"extracted {len(text)} chars from first 2 pages")
            print("--- first 3000 chars ---")
            print(text[:3000])


if __name__ == "__main__":
    main()
