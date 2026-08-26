"""One-off debug helper: check whether MicroVal's list tables (view-microval,
view-microval-confirmation) are client-side paginated by jQuery DataTables,
and if so, how many real rows exist in total vs. how many
microval_live_fetch.py's current page.content() snapshot actually captures.

Prompted by the project owner noticing a "Next" pagination control on the
real site (25 rows/page, 4+ pages visible) and finding it suspicious that
only 32 total MicroVal certificates had been mined so far -- which is
exactly what you'd get if the scraper's single page.content() call after
load only captures whatever DataTables has attached to the live DOM for
the *current* page, not the full underlying dataset it holds in memory.

Uses the DataTable's own JS API (window.jQuery('table.dataTable').DataTable())
rather than guessing at pagination-button selectors: `.page.info()` reports
the real recordsTotal/recordsDisplay/pages, and `.data()` returns the raw
per-row cell data for every row DataTables holds, regardless of which page
is currently rendered -- confirming (or ruling out) the undercount directly
instead of assuming it.

Only meant to be run ad hoc from a normal-egress environment (e.g. GitHub
Actions) -- nen.bettywebblocks.com is proxy-blocked from this repo's own
dev environment.
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

TARGET_URLS = {
    "view-microval": "https://nen.bettywebblocks.com/view-microval",
    "view-microval-confirmation": "https://nen.bettywebblocks.com/view-microval-confirmation",
}

PROBE_JS = """
() => {
  if (!window.jQuery || !jQuery.fn || !jQuery.fn.dataTable) {
    return {error: "jQuery/DataTables not found on window"};
  }
  const results = [];
  jQuery('table.dataTable').each(function () {
    const api = jQuery(this).DataTable();
    const info = api.page.info();
    const allRowsData = api.data().toArray();
    const domRowCount = jQuery(this).find('tbody tr').length;
    results.push({
      tableId: this.id,
      recordsTotal: info.recordsTotal,
      recordsDisplay: info.recordsDisplay,
      pageLength: info.length,
      pageCount: info.pages,
      domRowCountRightNow: domRowCount,
      allRowsDataCount: allRowsData.length,
      firstRowSample: allRowsData[0] || null,
      lastRowSample: allRowsData[allRowsData.length - 1] || null,
    });
  });
  return results;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug-dir", required=True)
    ap.add_argument("--timeout-ms", type=int, default=45000)
    args = ap.parse_args()

    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, url in TARGET_URLS.items():
            page = browser.new_page(user_agent="microbiology-methods-bot/1.0")
            print(f"[{label}] navigating to {url}", file=sys.stderr)
            try:
                page.goto(url, timeout=args.timeout_ms, wait_until="networkidle")
            except Exception as exc:  # noqa: BLE001
                print(f"[{label}] navigation warning: {exc}", file=sys.stderr)
            page.wait_for_timeout(2000)

            result = page.evaluate(PROBE_JS)
            (debug_dir / f"{label}_pagination_probe.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            print(f"[{label}] pagination probe result:\n{json.dumps(result, ensure_ascii=False, indent=2)}\n",
                  file=sys.stderr)

            page.close()
        browser.close()


if __name__ == "__main__":
    main()
