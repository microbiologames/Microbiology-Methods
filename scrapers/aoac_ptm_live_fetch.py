"""Live-fetch AOAC-RI Performance Tested Methods certificates from the
public validated-methods listing and feed each downloaded PDF through
aoac_ptm_parser.parse_certificate().

Confirmed against the real site across several CI runs, not guessed:
members.aoac.org/AOAC/AOAC/RI/PTM_Validated_Methods.aspx is a real,
reachable ASP.NET/Sitefinity page (real title "RI Validated Methods", no
login wall -- the earlier login-wall false positive on a generic "Sign In"
nav link has been fixed) that consistently returns 0 static PDF links to a
plain GET. Grepping the raw HTML found the actual mechanism: certificate
downloads go through a hidden-field + postback JS call
(Asi_WebRoot_AsiCommon_ContentManagement_DownloadDocument -- a hidden
"HiddenDownloadPathField" input plus a submit button), not
`<a href="*.pdf">` links a regex can see. When the plain GET finds nothing,
playwright_reconnaissance() drives headless Chromium instead (the same
approach that worked for MicroVal): render the page, capture HTML/
screenshot/JSON responses, and make one best-effort generic-row extraction
attempt -- reconnaissance to inform the next iteration, not a finished
parser guessed at blind. Still open as of the last run: whether the results
grid populates by default or needs an interaction (e.g. clicking a "Search"
button) to load -- check the debug dump's rendered_listing.html/png before
assuming which.

Usage:
    python3 aoac_ptm_live_fetch.py --out-dir data/aoac_ptm --debug-dir /tmp/aoac_debug
"""
import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aoac_ptm_parser import parse_certificate  # noqa: E402

LISTING_URL = "https://members.aoac.org/AOAC/AOAC/RI/PTM_Validated_Methods.aspx"
HEADERS = {"User-Agent": "microbiology-methods-bot/1.0 (+https://github.com/microbiologames/Microbiology-Methods)"}


def looks_like_login_wall(html: str) -> bool:
    # A bare "sign in" / "please log in" text match is too broad: most
    # association sites (AOAC included, as the first real run confirmed)
    # carry a persistent "Sign In" nav link for members regardless of
    # whether the actual page content is public. Only an actual password
    # field is a reliable signal that this specific page is a login form.
    return 'type="password"' in html.lower()


def looks_js_rendered(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if not body:
        return True
    text = body.get_text(strip=True)
    structural_elements = body.find_all(["a", "td", "li", "tr"])
    # A real listing page, even a sparse one, has *some* links/rows -- an
    # almost-empty body with none of those (just a root div for a JS bundle
    # to mount into) is the classic SPA-shell tell, which is exactly what
    # microval.org itself turned out to be. Checking both signals avoids
    # flagging a genuinely sparse-but-real page as JS-rendered.
    return len(text) < 50 and len(structural_elements) == 0


def find_pdf_links(html: str, base_url: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") or "certificate" in href.lower():
            links.add(urljoin(base_url, href))
    return sorted(links)


def find_next_page_url(html: str, base_url: str, current_url: str):
    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.find("a", rel="next") or soup.find("a", string=re.compile(r'^\s*next\s*$', re.I))
    if next_link and next_link.get("href"):
        candidate = urljoin(base_url, next_link["href"])
        return candidate if candidate != current_url else None
    return None


def extract_generic_rows(html: str) -> list:
    """Same technique proven against MicroVal (see microval_live_fetch.py):
    repeated <tr> elements are the most reliable generic signal of a real
    data grid, whatever framework rendered it."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    return [
        [c.get_text(strip=True) for c in r.find_all(["th", "td"])]
        for r in rows if r.get_text(strip=True)
    ]


def playwright_reconnaissance(url: str, debug_dir, timeout_ms: int = 45000):
    """Fallback for when the plain GET-and-regex approach finds nothing: the
    listing page turned out (confirmed against the real site, not assumed)
    to be an ASP.NET/Sitefinity site whose certificate downloads go through
    a hidden-field + postback JS mechanism
    (Asi_WebRoot_AsiCommon_ContentManagement_DownloadDocument), not plain
    <a href="*.pdf"> links -- so this drives headless Chromium instead,
    mirroring the approach that worked for MicroVal: capture everything
    (rendered HTML, screenshot, JSON network responses) and make one
    best-effort generic-row extraction attempt, rather than a finished
    parser guessed at blind.
    """
    from playwright.sync_api import sync_playwright

    json_responses = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])

        def on_response(response):
            content_type = response.headers.get("content-type", "")
            if "json" in content_type.lower():
                try:
                    body = response.text()
                except Exception:  # noqa: BLE001 -- best-effort capture, never fatal
                    body = None
                json_responses.append({"url": response.url, "status": response.status, "body": body})

        page.on("response", on_response)

        print(f"[playwright] navigating to {url}", file=sys.stderr)
        try:
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        except Exception as exc:  # noqa: BLE001 -- still want to capture whatever loaded
            print(f"[playwright] navigation warning: {exc}", file=sys.stderr)
        page.wait_for_timeout(3000)  # let any lazy client-side rendering settle

        html = page.content()
        if debug_dir:
            (debug_dir / "rendered_listing.html").write_text(html, encoding="utf-8")
            page.screenshot(path=str(debug_dir / "rendered_listing.png"), full_page=True)
            if json_responses:
                (debug_dir / "rendered_listing_json_responses.json").write_text(
                    json.dumps(json_responses, ensure_ascii=False, indent=2), encoding="utf-8",
                )
        browser.close()

    pdf_links = find_pdf_links(html, url)
    rows = extract_generic_rows(html)
    print(f"[playwright] rendered page: {len(pdf_links)} pdf link(s), {len(rows)} generic table row(s), "
          f"{len(json_responses)} JSON network response(s) captured.", file=sys.stderr)
    return pdf_links, rows, json_responses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/aoac_ptm")
    ap.add_argument("--debug-dir", default=None, help="If set, dump each fetched listing page's raw HTML here for diagnosis.")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--delay-seconds", type=float, default=2.0)
    ap.add_argument("--skip-playwright-fallback", action="store_true",
                     help="Don't fall back to a headless-browser reconnaissance pass when the plain GET finds 0 PDF links.")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    pdf_links = set()
    url = LISTING_URL
    seen_pages = set()
    for page_num in range(1, args.max_pages + 1):
        if not url or url in seen_pages:
            break
        seen_pages.add(url)
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"FETCH ERROR [{url}]: {exc}", file=sys.stderr)
            break

        if debug_dir:
            (debug_dir / f"listing_page_{page_num}.html").write_text(resp.text, encoding="utf-8", errors="ignore")

        title_m = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.I | re.S)
        print(f"Page {page_num} ({url}): HTTP {resp.status_code}, "
              f"{len(resp.text)} chars, title={title_m.group(1).strip() if title_m else '(none)'!r}",
              file=sys.stderr)

        if looks_like_login_wall(resp.text):
            print(f"WARNING: page {page_num} looks like a login wall -- aborting. "
                  f"AOAC's listing may require authentication.", file=sys.stderr)
            break
        if looks_js_rendered(resp.text):
            print(f"WARNING: page {page_num} looks JS-rendered (near-empty body) -- "
                  f"a plain HTTP GET won't see real content here. This listing likely "
                  f"needs a headless-browser fetch instead (see microval_live_fetch.py "
                  f"for that pattern).", file=sys.stderr)
            break

        found = find_pdf_links(resp.text, url)
        print(f"Page {page_num} ({url}): {len(found)} PDF links found", file=sys.stderr)
        pdf_links.update(found)

        next_url = find_next_page_url(resp.text, url, url)
        url = next_url
        if url:
            time.sleep(args.delay_seconds)

    if not pdf_links:
        print("No certificate PDF links found via plain HTTP GET. Confirmed on real runs: "
              "this listing's downloads go through a JS postback mechanism a plain GET can't "
              "trigger, not that the page is empty -- falling back to a headless-browser "
              "reconnaissance pass.", file=sys.stderr)
        if not args.skip_playwright_fallback:
            pdf_links_pw, rows, _json_responses = playwright_reconnaissance(LISTING_URL, debug_dir)
            pdf_links.update(pdf_links_pw)
            if rows and debug_dir:
                (debug_dir / "rendered_listing_rows.json").write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
                )
            if rows:
                print(f"Found {len(rows)} generic table row(s) in the rendered page -- "
                      f"see rendered_listing_rows.json in --debug-dir. These are NOT yet "
                      f"parsed into certificates (structure needs a human to look at it "
                      f"first, the same way MicroVal's table structure was confirmed before "
                      f"writing its normalizer).", file=sys.stderr)
        if not pdf_links:
            print("Still no certificate PDF links found after the headless-browser pass. "
                  "Check the debug HTML/screenshot dump (--debug-dir) to see what the "
                  "listing page actually rendered.", file=sys.stderr)
            return

    parsed = failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, pdf_url in enumerate(sorted(pdf_links)):
            try:
                resp = session.get(pdf_url, timeout=60)
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(f"FETCH ERROR [{pdf_url}]: {exc}", file=sys.stderr)
                failed += 1
                continue

            pdf_path = tmp_path / f"cert_{i}.pdf"
            pdf_path.write_bytes(resp.content)

            try:
                rec = parse_certificate(pdf_path)
            except Exception as exc:  # noqa: BLE001 -- one bad PDF must not abort the whole run
                print(f"PARSE ERROR [{pdf_url}]: {exc}", file=sys.stderr)
                failed += 1
                continue

            if not rec.get("certificate_number"):
                print(f"WARNING: no certificate number extracted from {pdf_url}; skipping.", file=sys.stderr)
                failed += 1
                continue

            fname = re.sub(r'[^A-Za-z0-9]+', '_', rec["certificate_number"]).strip('_') + ".json"
            (out_dir / fname).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            parsed += 1
            time.sleep(args.delay_seconds)

    print(f"Fetched {len(pdf_links)} PDFs, parsed {parsed}, failed {failed} -> {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
