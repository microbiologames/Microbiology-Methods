"""URL handling shared by every path that downloads a source document.

This exists because the same bug was fixed once and then bit again. AFNOR
names a good many reports "N°16_..._DelvotestT.pdf", and Python's
urllib.request refuses a non-ASCII URL with UnicodeEncodeError instead of
escaping it. The fix went into pipeline/extract_expert_labs.py, where it
recovered 11 reports that had been silently skipped on every pass -- and
scrapers/backfill_llm_performance.py, which downloads the same documents
through urllib, still had the original bug. It cost six records their place
in the 77-record batch.

So the fix lives in one importable place now rather than in whichever
module happened to hit it first.

(requests, used by pipeline/fetch_and_mine_summary_reports.py, escapes
these URLs itself -- that path was never affected, which is part of why
this went unnoticed.)
"""
from urllib.parse import quote, urlsplit, urlunsplit


def encode_url(url: str) -> str:
    """Percent-encode the characters urllib.request will not send.

    safe="/%" leaves an already-escaped URL alone rather than turning its
    %20 into %2520.
    """
    parts = urlsplit(url)
    return urlunsplit(parts._replace(
        path=quote(parts.path, safe="/%"),
        query=quote(parts.query, safe="=&%"),
    ))
