"""Free (no Anthropic API call) diagnostic for reports where
llm_report_miner.py returned an empty/placeholder extraction.

Budget is currently frozen after the first real backfill batch spent ~$25
against a low API balance, without getting through the backlog -- see the
2026-08-27 batch log. Before spending any more credits, this checks the
one hypothesis that's free to test: that the "placeholder" failures are the
same root cause already fixed once for a different report (permissions
encryption pypdf can decrypt with an empty password), rather than a new
failure mode that would need more prompt/schema work (i.e. more paid runs)
to fix.

For each URL: downloads the PDF, reports pypdf's encryption status/algorithm,
whether decrypt("") succeeds, page count, and how much text pypdf itself can
extract per page after decryption (a proxy for "is this a normal digital PDF
pypdf/Claude can read" vs. "this is a scanned image with no text layer,"
which would need OCR -- a different fix than encryption).

Usage:
    python3 diagnose_llm_failures.py --urls-file urls.txt
"""
import argparse
import sys
import tempfile
import urllib.request
from pathlib import Path

import pypdf


def diagnose_one(pdf_path: Path) -> dict:
    reader = pypdf.PdfReader(str(pdf_path))
    info = {
        "is_encrypted": reader.is_encrypted,
        "decrypt_result": None,
        "num_pages": None,
        "text_chars_per_page": [],
    }
    if reader.is_encrypted:
        try:
            result = reader.decrypt("")
            info["decrypt_result"] = str(result)
        except Exception as exc:  # noqa: BLE001
            info["decrypt_result"] = f"EXCEPTION: {exc}"
            return info
    try:
        info["num_pages"] = len(reader.pages)
        for page in reader.pages[:3]:
            text = page.extract_text() or ""
            info["text_chars_per_page"].append(len(text))
    except Exception as exc:  # noqa: BLE001
        info["read_error"] = str(exc)
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls-file", required=True, help="One URL per line, optionally 'label|url'.")
    args = ap.parse_args()

    lines = [l.strip() for l in Path(args.urls_file).read_text().splitlines() if l.strip()]
    for line in lines:
        label, _, url = line.partition("|")
        if not url:
            url, label = label, label
        print(f"\n=== {label} ===", file=sys.stderr)
        print(url, file=sys.stderr)
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                urllib.request.urlretrieve(url, tmp.name)
                info = diagnose_one(Path(tmp.name))
            for k, v in info.items():
                print(f"  {k}: {v}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  DOWNLOAD/READ ERROR: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
