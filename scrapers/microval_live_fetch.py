"""Live fetch + parse for MicroVal certificates.

microval.org/en/issued-certificates/ is only a static shell: the real
certificate list loads inside an <iframe> whose src is set by JavaScript to
one of two Betty Blocks-hosted pages (confirmed by inspecting the one saved
copy of that shell page available to this project):
    https://nen.bettywebblocks.com/view-microval
    https://nen.bettywebblocks.com/view-microval-confirmation
Betty Blocks is a low-code platform whose pages are client-rendered, so a
plain `requests.get` -- as tried for microval.org itself -- returns an empty
shell. This script instead drives headless Chromium (Playwright) to actually
render the page.

This started as pure reconnaissance (this codebase's dev environment can't
reach nen.bettywebblocks.com, and no sample of the real rendered content was
ever available to develop against), capturing rendered HTML, a screenshot,
and any JSON network responses on the chance the data came from a
discoverable API. The first real run (from the project's GitHub Actions
workflow) settled the question: no useful API call -- the one JSON response
seen on both pages is just the jQuery DataTables plugin's i18n string file
(`cdn.datatables.net/.../English.json`, pagination labels), not certificate
data. The real data is a genuine server-rendered <table> (DataTables always
progressively-enhances real markup, never a div-based fake table), with a
consistent 6-column header confirmed identical across both pages: Analyte /
Certificate number / Test kit name / Supplier - manufacturer / Expiry date /
Status. extract_table_rows() reads that structure directly, per-<td>, rather
than joining a row's text into one string and hoping to split it back apart
later -- which would be genuinely ambiguous, since both the test-kit-name and
supplier fields are free multi-word text with no fixed boundary between them.
A row whose cell count doesn't match the header is kept as a single
joined-text fallback rather than guessing a split.

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
        print(f"[{label}] captured {len(json_responses)} JSON network response(s) in "
              f"{label}_json_responses.json -- on the two real runs so far this was just "
              f"DataTables' i18n strings file, not certificate data, but kept for the record "
              f"in case a real data API shows up on a future run.", file=sys.stderr)

    body_text = page.evaluate("document.body ? document.body.innerText : ''")
    browser.close()
    return html, body_text, json_responses


EXPECTED_HEADER = ["Analyte", "Certificate number", "Test kit name", "Supplier - manufacturer", "Expiry date", "Status"]


def extract_table_rows(html: str):
    """MicroVal's real tables (confirmed against actual captured pages,
    see scrapers/microval_live_fetch.py's module docstring) are rendered by
    jQuery DataTables -- which always progressively-enhances a genuine
    <table><tr><td> structure, never a div-based fake table -- with a
    consistent 6-column header: Analyte / Certificate number / Test kit
    name / Supplier - manufacturer / Expiry date / Status.

    Returns (header_cells, data_rows) where data_rows is a list of lists of
    per-<td> cell text (structure preserved, not joined into one string).
    A row whose cell count doesn't match the header's is kept as a single
    joined-text fallback item instead of silently mis-splitting it.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    def cell_text(cell) -> str:
        # get_text(strip=True) (no separator) concatenates a cell's text
        # nodes with nothing between them -- harmless for a single text
        # node, but confirmed against real captured data to silently
        # glue words together when a cell wraps across a <br> or nested
        # span (e.g. "Bacillus cereus<br>group" -> "Bacillus cereusgroup",
        # producing a second, near-duplicate organism downstream that
        # only differs by a missing space). Joining with an explicit
        # space and collapsing repeats keeps single-text-node cells
        # identical while fixing the multi-node case.
        return re.sub(r'\s+', ' ', cell.get_text(" ", strip=True)).strip()

    rows = soup.find_all("tr")
    if not rows:
        return [], []

    header_cells = [cell_text(c) for c in rows[0].find_all(["th", "td"])]
    data_rows = []
    for r in rows[1:]:
        cells = [cell_text(c) for c in r.find_all(["th", "td"])]
        if not any(cells):
            continue
        if len(cells) != len(header_cells):
            # Structure didn't match what we expected -- keep the row as a
            # single string rather than guess a misaligned split.
            data_rows.append([cell_text(r)])
        else:
            data_rows.append(cells)
    return header_cells, data_rows


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

    total_certs = 0
    with sync_playwright() as p:
        for label, url in TARGET_URLS.items():
            html, body_text, json_responses = capture_page(p, url, label, debug_dir, args.timeout_ms)

            header, rows = extract_table_rows(html)
            print(f"[{label}] table extraction: header={header!r}, {len(rows)} data row(s); "
                  f"body text length {len(body_text)} chars.", file=sys.stderr)

            if header != EXPECTED_HEADER:
                print(f"[{label}] WARNING: header doesn't match the expected MicroVal columns "
                      f"({EXPECTED_HEADER!r}) -- site layout may have changed; falling back to "
                      f"whatever columns were actually found.", file=sys.stderr)

            records = []
            for cells in rows:
                if len(cells) == len(header) == 6:
                    rec = dict(zip(
                        ["analyte_raw", "certificate_number", "commercial_name", "manufacturer_raw", "expiry_date_raw", "status_raw"],
                        cells,
                    ))
                else:
                    rec = {"unparsed_row": cells[0] if cells else ""}
                rec["source"] = "MICROVAL"
                rec["label"] = label
                rec["source_page_url"] = url
                records.append(rec)
                fname_key = re.sub(r'[^A-Za-z0-9]+', '_', rec.get("certificate_number") or f"row{len(records)}").strip('_')
                (out_dir / f"{label}--{fname_key}.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8",
                )
            total_certs += len(records)
            print(f"[{label}] wrote {len(records)} certificate record(s) -> {out_dir}", file=sys.stderr)

    print(f"Done: {total_certs} MicroVal certificate(s) total -> {out_dir}. "
          f"Debug HTML/screenshots/JSON still in {debug_dir} for verification.", file=sys.stderr)


if __name__ == "__main__":
    main()
