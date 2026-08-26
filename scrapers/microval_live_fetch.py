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
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

TARGET_URLS = {
    "view-microval": "https://nen.bettywebblocks.com/view-microval",
    "view-microval-confirmation": "https://nen.bettywebblocks.com/view-microval-confirmation",
}


# DataTables paginates client-side. Confirmed by a real probe (workflow
# run 32986700898, using each table's own DataTables JS API) that the main
# view-microval table alone holds 85 real certificates across 4 pages of
# 25 -- while a plain page.content() snapshot only ever captures whichever
# 25 happen to be attached to the live DOM for the currently-displayed
# page, which is exactly why this scraper had only ever found 32 total
# certificates (25 + the confirmation table's real, complete count of 7).
# `api.data().toArray()` returns every row DataTables holds in memory
# regardless of which page is rendered, so this reconstructs a single
# full <table> HTML string covering ALL rows -- same real header markup,
# same per-cell HTML DataTables was given originally -- and hands it to
# extract_table_rows() exactly as before, so that function needed no
# changes at all.
_DATATABLES_FULL_TABLE_JS = """
() => {
    if (!window.jQuery || !jQuery.fn || !jQuery.fn.dataTable) return null;
    const tables = jQuery('table.dataTable');
    if (tables.length === 0) return null;
    const table = tables.first();
    const api = table.DataTable();
    const headHtml = table.find('thead').prop('outerHTML') || '';
    const rowsHtml = api.data().toArray().map(
        row => '<tr>' + row.map(cell => '<td>' + cell + '</td>').join('') + '</tr>'
    ).join('');
    return '<table>' + headHtml + '<tbody>' + rowsHtml + '</tbody></table>';
}
"""


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

    full_table_html = page.evaluate(_DATATABLES_FULL_TABLE_JS)
    if full_table_html:
        (debug_dir / f"{label}_full_table.html").write_text(full_table_html, encoding="utf-8")
    else:
        print(f"[{label}] WARNING: no DataTables table found via the JS API -- "
              f"falling back to the plain page snapshot, which may only cover the "
              f"first page of a paginated table.", file=sys.stderr)

    body_text = page.evaluate("document.body ? document.body.innerText : ''")
    browser.close()
    return html, body_text, json_responses, full_table_html


EXPECTED_HEADER = ["Analyte", "Certificate number", "Test kit name", "Supplier - manufacturer", "Expiry date", "Status"]

# Confirmed by the project owner navigating the live site by hand, then
# verified directly (real dumped page text + link hrefs, see git history
# for the diagnostic script this replaced): each certificate has its own
# detail view at this URL, reachable by clicking a row in the list page
# (loaded inside microval.org's iframe, per the module docstring) but just
# as reachable by navigating Playwright straight to it -- the same
# shortcut already taken for the list page itself.
DETAIL_URL_TEMPLATE = "https://nen.bettywebblocks.com/view-microval-details?cert_nr={cert_nr}&r_name=MicroVal"

# Real fields confirmed present on every detail page dumped so far. "Study
# report" is the one that's conditional -- present only when MicroVal has
# actually published one (confirmed absent on a certificate the project
# owner had already checked by hand shows no report), which is exactly
# the signal extract_detail_fields() uses to tell "not published" apart
# from "we failed to find it".
_DETAIL_LABELS = [
    "Test kit name", "Supplier - manufacturer", "Analyte", "Matrices",
    "Reference method", "Certificate number", "First approval date",
    "Expiry date", "Status", "Certificate issued by",
]


def extract_detail_fields(html: str) -> dict:
    """Parse a MicroVal certificate detail page into a flat dict of its
    real fields plus certificate_pdf_url / summary_report_pdf_url.

    Confirmed structure (from real dumped pages, see git history for the
    diagnostic dump this was built against): a 2-column key/value table --
    <tr> rows each holding exactly one label cell and one value cell,
    the same DataTables-style real-<table> rendering already confirmed for
    the list page in extract_table_rows() above, not a div-based layout.
    The "Certificate" row's value is always a "Download" link (the
    certificate document itself, not validation data); "Study report" is
    the same shape but only appears as its own row when MicroVal has
    actually published one -- its absence is read as
    summary_report_pdf_url=None, not as an extraction failure.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    def cell_text(cell) -> str:
        return re.sub(r'\s+', ' ', cell.get_text(" ", strip=True)).strip()

    fields = {}
    certificate_pdf_url = None
    summary_report_pdf_url = None
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) != 2:
            continue
        label = cell_text(cells[0])
        link = cells[1].find("a", href=True)
        if label == "Certificate" and link:
            certificate_pdf_url = link["href"]
        elif label == "Study report" and link:
            summary_report_pdf_url = link["href"]
        elif label in _DETAIL_LABELS:
            fields[label] = cell_text(cells[1])

    missing = [lbl for lbl in _DETAIL_LABELS if lbl not in fields]
    if missing:
        print(f"WARNING: detail page missing expected field(s) {missing} -- "
              f"page structure may have changed.", file=sys.stderr)

    return {
        "matrices_raw": fields.get("Matrices"),
        "reference_method_raw": fields.get("Reference method"),
        "first_approval_date_raw": fields.get("First approval date"),
        "certificate_issued_by": fields.get("Certificate issued by"),
        "certificate_pdf_url": certificate_pdf_url,
        "summary_report_pdf_url": summary_report_pdf_url,
    }


def fetch_certificate_detail(browser, cert_nr: str, url: str, debug_dir: Path, timeout_ms: int) -> dict:
    # Takes an already-launched browser (one launch reused across all real
    # certificates, rather than relaunching Chromium per certificate) and
    # opens a fresh page/tab per call. `url` is the row's own captured
    # detail-page link when there was one (see extract_table_rows), since
    # that link's URL scheme isn't the same for every MicroVal list page.
    page = browser.new_page(user_agent="microbiology-methods-bot/1.0")
    try:
        page.goto(url, timeout=timeout_ms, wait_until="networkidle")
    except Exception as exc:  # noqa: BLE001 -- still want to capture whatever loaded
        print(f"[{cert_nr}] detail page navigation warning: {exc}", file=sys.stderr)
    page.wait_for_timeout(1000)
    html = page.content()
    (debug_dir / f"detail--{cert_nr}.html").write_text(html, encoding="utf-8")
    page.close()
    return extract_detail_fields(html)


def extract_table_rows(html: str):
    """MicroVal's real tables (confirmed against actual captured pages,
    see scrapers/microval_live_fetch.py's module docstring) are rendered by
    jQuery DataTables -- which always progressively-enhances a genuine
    <table><tr><td> structure, never a div-based fake table -- with a
    consistent 6-column header: Analyte / Certificate number / Test kit
    name / Supplier - manufacturer / Expiry date / Status.

    Returns (header_cells, data_rows, row_links) where data_rows is a list
    of lists of per-<td> cell text (structure preserved, not joined into
    one string) and row_links is a same-length list of each row's
    Certificate-number-cell href (or None). Capturing that href matters
    because it turned out NOT to be uniform: confirmed against a real
    captured view-microval-confirmation.html, that page's Certificate
    number cell links straight to
    "/view-microval-confirmation-details/<id>" -- a different, numeric-ID
    URL scheme, not the "view-microval-details?cert_nr=..." pattern the
    main view-microval list (and the project owner's own manual browsing)
    uses. Reading the real href per row, rather than always constructing
    one from the certificate number, is what makes the certificate-detail
    fetch below work for both list types instead of only the first.
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
        return [], [], []

    header_cells = [cell_text(c) for c in rows[0].find_all(["th", "td"])]
    cert_col = header_cells.index("Certificate number") if "Certificate number" in header_cells else None
    data_rows = []
    row_links = []
    for r in rows[1:]:
        raw_cells = r.find_all(["th", "td"])
        cells = [cell_text(c) for c in raw_cells]
        if not any(cells):
            continue
        if len(cells) != len(header_cells):
            # Structure didn't match what we expected -- keep the row as a
            # single string rather than guess a misaligned split.
            data_rows.append([cell_text(r)])
            row_links.append(None)
        else:
            data_rows.append(cells)
            link = None
            if cert_col is not None:
                a = raw_cells[cert_col].find("a", href=True)
                if a:
                    link = a["href"]
            row_links.append(link)
    return header_cells, data_rows, row_links


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
    written_paths = []  # (path, record) -- revisited below to merge in detail-page fields
    with sync_playwright() as p:
        for label, url in TARGET_URLS.items():
            html, body_text, json_responses, full_table_html = capture_page(p, url, label, debug_dir, args.timeout_ms)

            # Prefer the reconstructed all-rows table (see capture_page) --
            # falling back to the plain page snapshot only if the
            # DataTables JS API wasn't found, which would otherwise
            # silently under-count a paginated table.
            header, rows, row_links = extract_table_rows(full_table_html or html)
            print(f"[{label}] table extraction: header={header!r}, {len(rows)} data row(s); "
                  f"body text length {len(body_text)} chars.", file=sys.stderr)

            if header != EXPECTED_HEADER:
                print(f"[{label}] WARNING: header doesn't match the expected MicroVal columns "
                      f"({EXPECTED_HEADER!r}) -- site layout may have changed; falling back to "
                      f"whatever columns were actually found.", file=sys.stderr)

            records = []
            for cells, row_link in zip(rows, row_links):
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
                # The row's own Certificate-number-cell link, if it had
                # one, resolved against this page's own origin -- see
                # extract_table_rows for why this can't just be built from
                # the certificate number for every list page.
                rec["detail_page_url"] = urljoin(url, row_link) if row_link else None
                records.append(rec)
                fname_key = re.sub(r'[^A-Za-z0-9]+', '_', rec.get("certificate_number") or f"row{len(records)}").strip('_')
                out_path = out_dir / f"{label}--{fname_key}.json"
                out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                written_paths.append((out_path, rec))
            total_certs += len(records)
            print(f"[{label}] wrote {len(records)} certificate record(s) -> {out_dir}", file=sys.stderr)

        # Second pass: the list page only ever gave 6 columns (no matrices,
        # no report link -- see module docstring). Each certificate has its
        # own detail page with those real fields (confirmed by the project
        # owner navigating the live site by hand, then verified directly);
        # fetch it for every real certificate found above and merge the
        # result into that same record file.
        detail_targets = [(path, rec) for path, rec in written_paths if rec.get("certificate_number")]
        print(f"Fetching detail pages for {len(detail_targets)} certificate(s)...", file=sys.stderr)
        browser = p.chromium.launch()
        detail_ok = detail_failed = 0
        for out_path, rec in detail_targets:
            cert_nr = rec["certificate_number"]
            # Prefer the row's own captured detail link (real evidence: on
            # a first real run, every certificate from the "view-microval-
            # confirmation" list page came back with no detail fields at
            # all, because that page's rows link to a completely different
            # URL scheme -- /view-microval-confirmation-details/<id> -- not
            # the cert_nr-based one below, which is only what the main
            # view-microval list actually uses).
            url = rec.get("detail_page_url") or DETAIL_URL_TEMPLATE.format(cert_nr=cert_nr)
            try:
                detail = fetch_certificate_detail(browser, cert_nr, url, debug_dir, args.timeout_ms)
            except Exception as exc:  # noqa: BLE001 -- one bad detail page must not abort the whole batch
                print(f"[{cert_nr}] detail fetch error: {exc}", file=sys.stderr)
                detail_failed += 1
                continue
            rec.update(detail)
            out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            detail_ok += 1
        browser.close()
        print(f"Detail pages: {detail_ok} fetched, {detail_failed} failed.", file=sys.stderr)

    print(f"Done: {total_certs} MicroVal certificate(s) total -> {out_dir}. "
          f"Debug HTML/screenshots/JSON still in {debug_dir} for verification.", file=sys.stderr)


if __name__ == "__main__":
    main()
