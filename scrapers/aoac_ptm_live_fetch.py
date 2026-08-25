"""Live-fetch AOAC-RI Performance Tested Methods certificates from the
public validated-methods listing and feed each downloaded PDF through
aoac_ptm_parser.parse_certificate().

UNVERIFIED against the real site: this codebase's development environment
cannot reach members.aoac.org (egress-blocked), so the HTML-scraping logic
below is a best-effort design based only on the 4 example certificate PDFs
supplied manually (all direct PDF downloads, named like
"022001C_AppliedFoodTop7STEC.pdf") -- not on ever having seen the actual
listing page's markup. Expect this to need adjustment on its first real run;
that's why every step logs what it found (or didn't) rather than failing
silently, and the workflow step this runs in uploads a raw-HTML debug dump
as an artifact.

Known unknowns, called out explicitly rather than guessed around:
  - Whether the listing is static HTML or JS-rendered (if the latter, this
    plain-requests approach will find nothing and the debug dump will show
    a near-empty body -- that's the signal to switch to a Playwright-based
    fetch like microval_live_fetch.py instead).
  - Whether results are paginated via query-string params, a "next" link,
    or a POST-based ASP.NET postback (__VIEWSTATE) that a simple GET can't
    follow -- only the first page/response is fetched for now.
  - Whether members.aoac.org gates any of this behind a login -- if the
    response looks like a login page (short body, a password field), this
    is logged explicitly rather than treated as "zero certificates found".

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/aoac_ptm")
    ap.add_argument("--debug-dir", default=None, help="If set, dump each fetched listing page's raw HTML here for diagnosis.")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--delay-seconds", type=float, default=2.0)
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
        print("No certificate PDF links found. Check the debug HTML dump (--debug-dir) "
              "to see what the listing page actually returned.", file=sys.stderr)
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
