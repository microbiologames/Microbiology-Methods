"""Which pages of a 100-page report get sent to the API, and which don't.

Sending the whole PDF is what made the first backfill cost ~$0.40 a report:
a native-PDF request bills each page's text plus a rendered image of it.
select_pages cuts that to the pages that can actually carry the answer.

The risk it introduces is the reason for this file. Dropping the page the
table was on turns a working extraction into a silent wrong answer -- worse
than the cost it saves. So the cases below are mostly about what must NOT be
dropped: the cover, a table's continuation page, and every page of a report
we cannot make sense of.

Run: python3 tests/test_page_selection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scrapers"))
from llm_report_miner import (  # noqa: E402
    MAX_PAGES_SENT, _format_pages, _score_page, select_pages,
)

COVER = "NF VALIDATION\nSummary report\nCertificate 3M 01/09-04/03\nValidated method"
PROSE = ("The study was carried out according to EN ISO 16140-2:2016 by the expert "
         "laboratory. Sensitivity of the alternative method was assessed across all "
         "food categories in an interlaboratory study organised in 2019.")
TABLE = ("Table 4 - Relative trueness per category\n"
         "Category Bias Standard deviation 95% CI\n"
         "Meat products -0.12 0.34 -0.28 0.04\n"
         "Dairy products 0.07 0.29 -0.11 0.25\n"
         "Fishery products -0.03 0.41 -0.22 0.16\n"
         "Acceptability limit 0.50")
TABLE_CONT = ("Fruit and vegetables 0.19 0.37 -0.02 0.40\n"
              "Multi-component foods -0.08 0.31 -0.25 0.09\n"
              "Infant formula 0.02 0.28 -0.14 0.18")
FILLER = "Annex C - Packaging and storage instructions for the test kit. See notice."


class _Page:
    def __init__(self, text): self._t = text
    def extract_text(self): return self._t


class _Reader:
    def __init__(self, texts): self.pages = [_Page(t) for t in texts]


def build(n=100, table_at=44):
    """A report shaped like the real ones: cover, prose, one table that runs
    over onto the next page, filler everywhere else."""
    texts = [FILLER] * n
    texts[0] = COVER
    texts[3] = PROSE
    texts[table_at] = TABLE
    texts[table_at + 1] = TABLE_CONT
    return _Reader(texts)


def main() -> int:
    failures = []

    def check(label, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    # --- scoring ---
    check("a results table outscores the prose that introduces it",
          _score_page(TABLE) > _score_page(PROSE),
          f"table={_score_page(TABLE)} prose={_score_page(PROSE)}")
    check("filler scores zero", _score_page(FILLER) == 0, str(_score_page(FILLER)))
    check("empty page scores zero", _score_page("") == 0)

    # --- selection on a realistic report ---
    pages = select_pages(build(), MAX_PAGES_SENT)
    check("cover page is always sent", 0 in pages)
    check("the table page is sent", 44 in pages)
    check("the table's continuation page is sent", 45 in pages)
    check("the page cap is respected", len(pages) <= MAX_PAGES_SENT, f"{len(pages)} pages")
    check("filler is dropped", len(pages) < 100, f"{len(pages)} pages")

    # --- the cases where dropping pages would be wrong ---
    small = select_pages(_Reader([FILLER] * 9), MAX_PAGES_SENT)
    check("a short report is sent whole", small == list(range(9)))

    blank = select_pages(_Reader([FILLER] * 60), MAX_PAGES_SENT)
    check("a report with no locatable table is sent whole, not guessed at",
          blank == list(range(60)), f"{len(blank)} pages")

    scanned = select_pages(_Reader([""] * 80), MAX_PAGES_SENT)
    check("an image-only PDF (no text layer) is sent whole",
          scanned == list(range(80)), f"{len(scanned)} pages")

    # --- a table near the end must survive the +1 expansion at the boundary ---
    edge = select_pages(build(n=50, table_at=48), MAX_PAGES_SENT)
    check("a table on the last two pages is kept", 48 in edge and 49 in edge)

    # --- several tables, as the Petrifilm reports have ---
    multi = build()
    multi.pages[60] = _Page(TABLE)
    multi.pages[61] = _Page(TABLE_CONT)
    got = select_pages(multi, MAX_PAGES_SENT)
    check("both tables are kept when a report has more than one",
          {44, 45, 60, 61} <= set(got), sorted(got))

    # --- formatting ---
    check("page ranges are collapsed for the log",
          _format_pages([0, 43, 44, 45, 59]) == "1, 44-46, 60",
          _format_pages([0, 43, 44, 45, 59]))

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===")
        for f in failures:
            print("  -", f)
        return 1

    sent = len(select_pages(build(), MAX_PAGES_SENT))
    print(f"=== all checks passed === (a 100-page report now sends {sent} pages, "
          f"{100 / sent:.0f}x less)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
