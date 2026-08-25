"""Reconnaissance + best-effort live fetch for MicroVal certificates.

microval.org/en/issued-certificates/ is only a static shell: the real
certificate list loads inside an <iframe> whose src is set by JavaScript to
one of two Betty Blocks-hosted pages (confirmed by inspecting the one saved
copy of that shell page available to this project):
    https://nen.bettywebblocks.com/view-microval
    https://nen.bettywebblocks.com/view-microval-confirmation
Betty Blocks is a low-code platform whose pages are typically client-rendered
(the list is fetched by JS after the initial HTML loads), so a plain
`requests.get` -- as tried for microval.org itself -- most likely returns an
empty shell too. This script instead drives headless Chromium (Playwright) to
actually render the page.

This has NEVER been run against the real site: this codebase's development
environment can't reach nen.bettywebblocks.com (egress-blocked), and no
sample of the real rendered content (only the outer shell page) has been
available to develop against. There is, by construction, no way to know the
real field layout (which column is the certificate number, product name,
manufacturer, etc.) without seeing it -- so this script deliberately does NOT
try to guess a mapping into the canonical schema. Instead it:
  1. Captures every angle that might help a human (or a follow-up parser)
     understand the real structure on the first run: full rendered HTML,
     a full-page screenshot, and the body of every JSON network response
     seen while the page loaded (Betty Blocks may well fetch its data from
     a discoverable API, which would be far easier to parse than scraped
     HTML -- worth capturing on the chance it's there).
  2. Makes one best-effort, clearly-flagged attempt at generic extraction:
     look for the largest repeated DOM structure (table rows, or sibling
     elements sharing a class) and dump their text content as raw,
     unmapped records -- useful as a starting point for writing the real
     parser once someone has looked at the debug output, not a finished
     collector.

Usage:
    pip install playwright && playwright install --with-deps chromium
    python3 microval_live_fetch.py --debug-dir /tmp/microval_debug --out-dir data/microval
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

TARGET_URLS = {
    "view-microval": "https://nen.bettywebblocks.com/view-microval",
    "view-microval-confirmation": "https://nen.bettywebblocks.com/view-microval-confirmation",
}


def capture_page(playwright, url: str, label: str, debug_dir: Path, timeout_ms: int):
    browser = playwright.chromium.launch()
    page = browser.new_page(user_agent="microbiology-methods-bot/1.0")

    json_responses = []

    def on_response(response):
        content_type = response.headers.get("content-type", "")
        if "json" in content_type.lower():
            try:
                body = response.text()
            except Exception:  # noqa: BLE001 -- best-effort capture, never fatal
                body = None
            json_responses.append({"url": response.url, "status": response.status, "body": body})

    page.on("response", on_response)

    print(f"[{label}] navigating to {url}", file=sys.stderr)
    try:
        page.goto(url, timeout=timeout_ms, wait_until="networkidle")
    except Exception as exc:  # noqa: BLE001 -- still want to capture whatever loaded
        print(f"[{label}] navigation warning: {exc}", file=sys.stderr)

    # Give any lazy client-side rendering a bit more time beyond networkidle.
    page.wait_for_timeout(2000)

    html = page.content()
    (debug_dir / f"{label}.html").write_text(html, encoding="utf-8")
    page.screenshot(path=str(debug_dir / f"{label}.png"), full_page=True)
    if json_responses:
        (debug_dir / f"{label}_json_responses.json").write_text(
            json.dumps(json_responses, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"[{label}] captured {len(json_responses)} JSON network response(s) -- "
              f"check {label}_json_responses.json first, it may be far easier to "
              f"parse than the rendered HTML.", file=sys.stderr)

    body_text = page.evaluate("document.body ? document.body.innerText : ''")
    browser.close()
    return html, body_text, json_responses


def best_effort_extract_rows(html: str) -> list:
    """Finds whichever tag ('tr' or a generic container) repeats the most
    with near-identical structure, and returns each instance's visible text
    split on whitespace runs -- a generic, unmapped starting point, not a
    real parser. Returns [] if nothing looks like a repeated list/table."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    rows = soup.find_all("tr")
    if len(rows) >= 3:
        return [re.sub(r'\s+', ' ', r.get_text(" ", strip=True)) for r in rows if r.get_text(strip=True)]

    # Fall back to the most-repeated class among direct children of any container.
    from collections import Counter
    class_counts = Counter()
    for el in soup.find_all(True, class_=True):
        class_counts[tuple(el.get("class"))] += 1
    if class_counts:
        top_class, count = class_counts.most_common(1)[0]
        if count >= 3:
            matches = soup.find_all(True, class_=list(top_class))
            return [re.sub(r'\s+', ' ', m.get_text(" ", strip=True)) for m in matches if m.get_text(strip=True)]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug-dir", required=True, help="Directory to dump rendered HTML, screenshots, and any captured JSON API responses.")
    ap.add_argument("--out-dir", default="data/microval")
    ap.add_argument("--timeout-ms", type=int, default=45000)
    args = ap.parse_args()

    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        for label, url in TARGET_URLS.items():
            html, body_text, json_responses = capture_page(p, url, label, debug_dir, args.timeout_ms)

            rows = best_effort_extract_rows(html)
            print(f"[{label}] best-effort extraction found {len(rows)} repeated row-like elements; "
                  f"body text length {len(body_text)} chars.", file=sys.stderr)

            record = {
                "source": "MICROVAL",
                "label": label,
                "url": url,
                "raw_rows_unmapped": rows,
                "had_json_api_responses": bool(json_responses),
                "provenance": {
                    "source_type": "reconnaissance_live_fetch",
                    "note": (
                        "Generic, unmapped extraction from a first real render of this page -- "
                        "no confirmed field layout exists yet. Check the debug HTML/screenshot/"
                        "JSON-response dump alongside this file before trusting raw_rows_unmapped "
                        "for anything; the real parser (mapping rows to certificate_number, "
                        "commercial_name, manufacturer, etc.) still needs to be written once a "
                        "human has looked at what this actually captured."
                    ),
                },
            }
            (out_dir / f"{label}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Done. Inspect {debug_dir} for raw HTML/screenshots/JSON before trusting {out_dir}.", file=sys.stderr)


if __name__ == "__main__":
    main()
