"""One-off debug helper: render a MicroVal certificate detail page
(https://nen.bettywebblocks.com/view-microval-details?cert_nr=...) with
headless Chromium and dump the HTML + a screenshot, so the real DOM
structure (field labels, table layout, the "Certificate" download link,
and -- when present -- a study/summary report link) can be seen before
writing a real extractor, instead of guessed at.

Confirmed by the project owner navigating the site by hand: this detail
view is reached by clicking a certificate row inside the view-microval
list page (itself loaded in an <iframe> on microval.org/en/issued-
certificates/, per microval_live_fetch.py's docstring) -- the outer
page's URL bar doesn't change because only the iframe navigates, which is
normal iframe behavior, not a client-side-routing quirk. Navigating
Playwright directly to the detail URL (bypassing the outer shell
entirely, the same shortcut microval_live_fetch.py already takes for the
list page) should render the same content directly.

Only meant to be run ad hoc from a normal-egress environment (e.g. GitHub
Actions) -- nen.bettywebblocks.com is proxy-blocked from this repo's own
dev environment.

Usage:
    python3 debug_dump_microval_detail.py --debug-dir /tmp/microval_detail_debug \
        2011LR41 2010LR38
"""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DETAIL_URL = "https://nen.bettywebblocks.com/view-microval-details?cert_nr={cert_nr}&r_name=MicroVal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cert_numbers", nargs="+")
    ap.add_argument("--debug-dir", required=True)
    ap.add_argument("--timeout-ms", type=int, default=45000)
    args = ap.parse_args()

    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for cert_nr in args.cert_numbers:
            url = DETAIL_URL.format(cert_nr=cert_nr)
            page = browser.new_page(user_agent="microbiology-methods-bot/1.0")
            print(f"[{cert_nr}] navigating to {url}", file=sys.stderr)
            try:
                page.goto(url, timeout=args.timeout_ms, wait_until="networkidle")
            except Exception as exc:  # noqa: BLE001 -- still want to capture whatever loaded
                print(f"[{cert_nr}] navigation warning: {exc}", file=sys.stderr)
            page.wait_for_timeout(2000)

            html = page.content()
            (debug_dir / f"{cert_nr}.html").write_text(html, encoding="utf-8")
            page.screenshot(path=str(debug_dir / f"{cert_nr}.png"), full_page=True)

            body_text = page.evaluate("document.body ? document.body.innerText : ''")
            print(f"[{cert_nr}] body text ({len(body_text)} chars):\n{body_text}\n", file=sys.stderr)

            # Every <a href> on the page -- the fastest way to see the exact
            # "Certificate" download link and (if present) a study/summary
            # report link's real URL pattern, without guessing at selectors.
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
            )
            print(f"[{cert_nr}] links: {links}\n", file=sys.stderr)

            page.close()
        browser.close()


if __name__ == "__main__":
    main()
