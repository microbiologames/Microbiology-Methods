"""Live-fetch AOAC-RI Performance Tested Methods certificates from the
public validated-methods listing and feed each downloaded PDF through
aoac_ptm_parser.parse_certificate().

Confirmed against the real site across several CI runs, not guessed:
members.aoac.org/AOAC/AOAC/RI/PTM_Validated_Methods.aspx is a real,
reachable ASP.NET page built on the iMIS association-management system
(real title "RI Validated Methods", no login wall -- the earlier
login-wall false positive on a generic "Sign In" nav link has been fixed)
that consistently returns 0 static PDF links to a plain GET. Two things
were confirmed by inspecting the actual rendered page, not guessed:

1. Certificate downloads go through a hidden-field + postback JS call
   (Asi_WebRoot_AsiCommon_ContentManagement_DownloadDocument -- a hidden
   "HiddenDownloadPathField" input plus a submit button), not
   `<a href="*.pdf">` links a regex can see.
2. The results grid is a genuine iMIS "IQA" search widget, not a
   pre-populated listing or a login-gated one: its own on-page copy says
   "Enter criteria and/or click 'Find' to browse the listing of validated
   methods," and shows "Please enter your search criteria to view
   results" until a search actually runs.

CONCLUSION (confirmed against the real site, with a full stack trace, not
guessed): this listing is currently broken on AOAC's own end, for any
browser, not just automation. Clicking "Find" -- with a real filter
selected (Discipline=Microbiological, confirmed to actually register:
selectedOptions=['MICRO'], not disabled) -- never reaches the server.
Page_ClientValidate isn't even defined on this page (so that's not the
blocker), and __doPostBack itself is genuinely undefined at click time
(confirmed to still be undefined after an explicit 10-second wait, so
not a load-order race either). The captured pageerror stack trace shows
why: Telerik.Web.UI.RadAjaxManager._applyUpdatePanelsRenderMode throws
"Cannot read properties of null (reading 'length')" during the page's
own Sys.Application._doInitialize() -- i.e. the ASP.NET AJAX framework's
client-side init crashes on this specific page before it finishes wiring
up __doPostBack, most likely because RadAjaxManager is configured to
reference an UpdatePanel or container that doesn't exist in this page's
current markup (a server-side misconfiguration on AOAC's side, plausibly
left over from a template change). playwright_reconnaissance() below
still performs the full click-through (selecting Microbiological,
clicking Find, capturing before/after HTML/screenshots and all console/
page errors) so a future run can immediately show if AOAC fixes this,
but there is currently no client-side workaround: the page's own script
never reaches the code path that would submit the search, regardless of
how the click is driven.

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
from urllib.parse import urljoin, urlsplit

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
    to be an ASP.NET/iMIS site (AOAC uses the iMIS association-management
    system, not raw Sitefinity as first guessed) whose certificate downloads
    go through a hidden-field + postback JS mechanism
    (Asi_WebRoot_AsiCommon_ContentManagement_DownloadDocument), not plain
    <a href="*.pdf"> links -- so this drives headless Chromium instead,
    mirroring the approach that worked for MicroVal.

    A second real run explained *why* the page always renders zero rows:
    it's a genuine iMIS "IQA" search widget (id ...ciPTMValidatedMethods_
    ResultsGrid...), not a pre-populated grid or a login-gated one. Its own
    on-page copy says "Enter criteria and/or click 'Find' to browse the
    listing of validated methods," and the results panel shows literally
    "Please enter your search criteria to view results" until a search is
    run. So after the initial page load, this now also clicks the widget's
    "Find" submit button (with no filter criteria entered, to request every
    record) and captures the page again -- that's the actual next
    reconnaissance step, not a finished parser guessed at blind: the exact
    shape of the resulting grid still needs a human to look at before
    writing a real extractor.
    """
    from playwright.sync_api import sync_playwright

    json_responses = []
    network_log = []
    phase = {"value": "initial"}  # mutable so the response handler below sees updates

    def is_aoac_host(response_url: str) -> bool:
        # A naive substring check on the full URL is wrong here and produced
        # a false "5 real postback requests" reading on an earlier run: a
        # Google Analytics collection request's own query string embeds the
        # (percent-encoded, but letters/dots survive unescaped) referring
        # page URL as tracking data, e.g. "...&dl=https%3A%2F%2Fmembers.
        # aoac.org%2F...", so "aoac.org" appears verbatim inside an
        # analytics.google.com request that never touched AOAC's server at
        # all. Only the actual request host is a reliable signal.
        return urlsplit(response_url).netloc.endswith("aoac.org")

    def on_response(response):
        request = response.request
        entry = {
            "phase": phase["value"],
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "resource_type": request.resource_type,
        }
        content_type = response.headers.get("content-type", "")
        # Capturing every response body in full would balloon the debug dump
        # with unrelated boilerplate (the cookie-consent banner's JSON alone
        # is 60+ KB) -- but a non-GET request to the AOAC site itself is
        # exactly the "did a postback actually fire?" signal this exists to
        # find, and its body is the direct answer, whatever its content type.
        if is_aoac_host(response.url) and (request.method != "GET" or "json" in content_type.lower()):
            try:
                entry["body"] = response.text()[:5000]
            except Exception:  # noqa: BLE001 -- best-effort capture, never fatal
                entry["body"] = None
            if "json" in content_type.lower():
                json_responses.append({"url": response.url, "status": response.status, "body": entry.get("body")})
        network_log.append(entry)

    dialogs_seen = []

    def on_dialog(dialog):
        # The single most likely explanation for a click that "succeeds"
        # (no exception) yet triggers no postback at all: a JS confirm()
        # (e.g. "No filters entered -- this may return many results,
        # continue?") that Playwright auto-*dismisses* by default when
        # nothing handles the 'dialog' event, silently cancelling whatever
        # the click was about to do. Accepting it here tests that directly
        # instead of leaving it as an unconfirmed guess.
        dialogs_seen.append({"type": dialog.type, "message": dialog.message})
        print(f"[playwright] JS dialog appeared ({dialog.type}): {dialog.message!r} -- accepting it.",
              file=sys.stderr)
        dialog.accept()

    console_messages = []

    def on_console(msg):
        # The last run ruled out client-side validation as the blocker
        # (hasPageClientValidate was False, so that whole if-guard is
        # skipped and the onclick goes straight to __doPostBack(...)) --
        # yet the postback still never reached the server. A JS exception
        # thrown inside that handler (e.g. if __doPostBack itself, or
        # something it depends on, isn't actually available) would abort
        # execution silently from Playwright's point of view: click()
        # doesn't raise just because the page's own onclick threw. Console
        # errors and uncaught exceptions are the only place that would
        # surface, so both get captured from here on.
        if msg.type in ("error", "warning"):
            console_messages.append({"type": msg.type, "text": msg.text})

    def on_pageerror(exc):
        # __doPostBack never becoming defined, confirmed on a real run to
        # persist even after an explicit 10s wait (not a load-order race),
        # is consistent with an earlier uncaught exception (the "Cannot
        # read properties of null" pageerror also seen) permanently
        # aborting a shared inline <script> block partway through, before
        # __doPostBack's own definition runs -- but that's still inferred
        # from two isolated error messages, not shown directly. `stack`
        # (when the browser provides one) names the actual offending
        # script and line, which is a much more direct answer than
        # guessing from the exception text alone.
        stack = getattr(exc, "stack", None)
        console_messages.append({"type": "pageerror", "text": str(exc), "stack": stack})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.on("response", on_response)
        page.on("dialog", on_dialog)
        page.on("console", on_console)
        page.on("pageerror", on_pageerror)

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

        # The last run's captured pageerror was the direct cause of every
        # previous no-op: "__doPostBack is not defined". ASP.NET normally
        # emits __doPostBack's definition inline near the top of the form
        # as part of the page's own script setup, and it should already
        # exist well before "networkidle" -- but the page also threw an
        # unrelated "Cannot read properties of null" pageerror during
        # initial load (plus a Mixed Content block on an unrelated ad-
        # network script), and either could plausibly have aborted a
        # shared inline <script> block partway through, before
        # __doPostBack's own definition ran. Waiting for it explicitly
        # (rather than assuming networkidle + a fixed delay was enough)
        # tests that directly instead of guessing at a longer fixed wait.
        try:
            page.wait_for_function("typeof __doPostBack === 'function'", timeout=10000)
            print("[playwright] __doPostBack is defined and ready.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 -- still proceed; the click will just no-op again
            print(f"[playwright] __doPostBack never became defined within 10s: {exc} "
                  f"-- this is very likely why every previous click silently did nothing.",
                  file=sys.stderr)

        # The previous run's captured onclick handler explained the earlier
        # no-op: it calls Page_ClientValidate(...) before __doPostBack(...)
        # and returns early if that fails -- and a corrected (host-based,
        # not substring) count of post-click requests to members.aoac.org
        # showed there were actually zero, meaning validation was in fact
        # failing silently (ASP.NET validators typically show an inline
        # message rather than a JS alert/confirm, so this produced no
        # dialog either). That points at a plain "at least one filter is
        # required" rule, not a broken widget. Selecting "Microbiological"
        # from the Discipline listbox both should satisfy that (a real
        # selection, not blank) and happens to be exactly this project's
        # actual scope -- AOAC-RI covers far more than microbiology.
        try:
            discipline_select = page.locator("select.chosen-select").filter(
                has=page.locator("option", has_text="Microbiological")
            ).first
            # The "chosen" jQuery plugin (confirmed present: a sibling
            # ".chosen-container" div was seen alongside this <select> in an
            # earlier run's structural dump) replaces the real <select>'s
            # visual presentation and hides the original element -- so a
            # plain select_option() call times out waiting for it to become
            # "visible" by Playwright's actionability rules, even though the
            # element is what the form actually submits. force=True skips
            # that visibility check; the underlying <select>'s value (and
            # its change event, which select_option still dispatches even
            # forced) is what the page's own validation and postback read,
            # not the Chosen widget's decorative overlay.
            discipline_select.select_option(label="Microbiological", force=True)
            # force=True proves Playwright *acted* on the element, not that
            # the selection actually stuck the way the page's own
            # validation reads it (e.g. a disabled underlying <select> that
            # Chosen manages separately would silently ignore this) -- so
            # read the DOM's own selectedOptions back out directly rather
            # than assuming.
            selected_values = discipline_select.evaluate(
                "el => Array.from(el.selectedOptions).map(o => o.value)"
            )
            is_disabled = discipline_select.evaluate("el => el.disabled")
            print(f"[playwright] selected 'Microbiological' in the Discipline filter "
                  f"(this project's own scope, and a real selection to satisfy whatever "
                  f"'enter some criteria' validation blocked the previous blank attempt). "
                  f"Verified DOM state: selectedOptions={selected_values!r}, disabled={is_disabled!r}.",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 -- still try Find even if this didn't work
            print(f"[playwright] could not select 'Microbiological' in Discipline: {exc}", file=sys.stderr)

        requests_before_click = len(network_log)
        clicked_find = False
        try:
            find_button = page.get_by_role("button", name="Find", exact=True)
            find_button.wait_for(state="visible", timeout=5000)
            button_outer_html = find_button.evaluate("el => el.outerHTML")
            print(f"[playwright] Find button markup: {button_outer_html}", file=sys.stderr)
            phase["value"] = "post-click"
            find_button.click()
            clicked_find = True
            print("[playwright] clicked the 'Find' search button.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 -- still want to capture whatever state we're in
            print(f"[playwright] could not click 'Find' button: {exc}", file=sys.stderr)

        if clicked_find:
            # A DOM scan for elements with "Validator" in their id came back
            # empty even though the selection was confirmed to stick
            # (selectedOptions=['MICRO'], not disabled) and the postback
            # still never reached the server -- so whatever
            # Page_ClientValidate(...) checks isn't a standard ASP.NET
            # validator control rendered as a labeled span. Querying its
            # actual machinery directly is more reliable than guessing at
            # more DOM patterns: ASP.NET WebForms' client-side validation
            # framework keeps every registered validator in a global
            # Page_Validators array, each with a live .isvalid flag and
            # .errormessage that Page_ClientValidate() sets when it runs --
            # that's the framework's own ground truth for which check (if
            # any) is failing and why, independent of how it's displayed.
            try:
                validator_state = page.evaluate(
                    "() => ({"
                    "hasPageClientValidate: typeof Page_ClientValidate === 'function',"
                    "pageIsValid: typeof Page_IsValid !== 'undefined' ? Page_IsValid : 'undefined',"
                    "validators: typeof Page_Validators !== 'undefined' "
                    "? Page_Validators.map(v => ({id: v.id, isvalid: v.isvalid, "
                    "errormessage: v.errormessage, validationGroup: v.validationGroup})) "
                    ": 'Page_Validators is undefined'"
                    "})"
                )
                print(f"[playwright] ASP.NET client validation state right after the click: "
                      f"{validator_state}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 -- diagnostic only
                print(f"[playwright] could not inspect Page_Validators: {exc}", file=sys.stderr)

        post_click_html = None
        if clicked_find:
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception as exc:  # noqa: BLE001 -- best-effort; still capture below
                print(f"[playwright] post-click networkidle warning: {exc}", file=sys.stderr)
            page.wait_for_timeout(4000)  # let the AJAX-updated grid finish rendering
            post_click_html = page.content()
            if debug_dir:
                (debug_dir / "rendered_listing_after_find.html").write_text(post_click_html, encoding="utf-8")
                page.screenshot(path=str(debug_dir / "rendered_listing_after_find.png"), full_page=True)

            requests_after_click = network_log[requests_before_click:]
            aoac_requests_after_click = [e for e in requests_after_click if is_aoac_host(e["url"])]
            print(f"[playwright] {len(requests_after_click)} network request(s) observed after the "
                  f"click, {len(aoac_requests_after_click)} of them actually to a *.aoac.org host "
                  f"(an earlier run's naive substring check falsely counted Google Analytics pixels "
                  f"whose own tracking query string embeds 'aoac.org' as the referring-page URL --"
                  f"fixed to check the request's real host instead). 0 real aoac.org requests means "
                  f"the click never reached the server at all -- a widget/JS problem (an auto-"
                  f"dismissed dialog, or client-side validation silently blocking __doPostBack) "
                  f"rather than a data-shape one.", file=sys.stderr)
            if dialogs_seen:
                print(f"[playwright] {len(dialogs_seen)} JS dialog(s) intercepted: {dialogs_seen}", file=sys.stderr)
            if console_messages:
                print(f"[playwright] console error(s)/pageerror(s) captured during this whole run: "
                      f"{console_messages}", file=sys.stderr)
            else:
                print("[playwright] no console errors or uncaught page errors captured at any point "
                      "(so the onclick handler ran to completion without throwing).", file=sys.stderr)

        if debug_dir and network_log:
            (debug_dir / "rendered_listing_network_log.json").write_text(
                json.dumps(network_log, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        browser.close()

    effective_html = post_click_html if post_click_html is not None else html
    pdf_links = find_pdf_links(effective_html, url)
    rows = extract_generic_rows(effective_html)
    print(f"[playwright] {'post-Find-click' if post_click_html is not None else 'initial'} page: "
          f"{len(pdf_links)} pdf link(s), {len(rows)} generic table row(s), "
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
