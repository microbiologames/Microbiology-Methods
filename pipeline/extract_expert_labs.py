"""Mine each summary report's opening pages for the expert laboratory that
ran the validation study, and for technology evidence on methods whose
detection principle is still unknown.

Free: no API involved, just an HTTP download and pdfplumber text extraction.
Deliberately reads only the first few pages of each report rather than the
whole document -- the expert lab is named in the cover page and in the
running header/footer of every page ("ADRIA Developpement ZA Creac'h Gwen
29000 Quimper", "Microsept Summary report - v0"), and the methods/protocol
section that names a thermocycler or an antibody conjugate is near the
front too. That matters practically: these reports run to 319 pages, and
the full-document mining step in scrape_afnor.yml really did run 1h38m
before being cancelled (run 32940563580), which is what blocks that
workflow from ever opening its PR. Reading 6 pages instead of 300 keeps
this bounded.

Two things are extracted in one download pass because the download is by
far the expensive part:

  1. study_design.expert_laboratory -- canonicalized through
     taxonomy.canonical_expert_lab so "ADRIA" and "ADRIA Developpement"
     land on one name.
  2. technology evidence for records whose method_type.category is still
     unknown, passed to taxonomy.canonical_method_category as study_text
     so a report describing a thermocycler classifies as molecular even
     when its product name gives nothing away.

Usage:
    python3 pipeline/extract_expert_labs.py --methods-dir data/methods
    python3 pipeline/extract_expert_labs.py --only-unclassified   # cheap pass
"""
import argparse
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import canonical_expert_lab, canonical_method_category  # noqa: E402

PAGES_TO_READ = 6
REQUEST_TIMEOUT = 60

# Phrases that introduce the expert lab on a real NF-Validation / MicroVal
# cover page. Captured group is the lab name; kept short and anchored so a
# sentence mentioning a collaborating lab elsewhere doesn't win.
_EXPERT_LAB_PATTERNS = [
    r'[Ee]xpert\s+laboratory\s*:?\s*([A-Z][\w\'\-À-ſ&. ]{2,60})',
    r'[Ll]aboratoire\s+expert\s*:?\s*([A-Z][\w\'\-À-ſ&. ]{2,60})',
    r'[Ss]tudy\s+(?:was\s+)?(?:carried\s+out|performed|conducted)\s+by\s+'
    r'(?:the\s+)?([A-Z][\w\'\-À-ſ&. ]{2,60})',
    r'[Ee]tude\s+r[ée]alis[ée]e\s+par\s*:?\s*([A-Z][\w\'\-À-ſ&. ]{2,60})',
]

# Fallback: the known labs' own names appearing anywhere in the opening
# pages. Reliable because these reports print the lab in the page header or
# footer of every page -- confirmed on real reports from both ADRIA
# ("ADRIA Developpement ... 29000 Quimper" in the page-1 letterhead) and
# Microsept ("Microsept / Summary report - v0" in the page footer).
_KNOWN_LAB_TOKENS = [
    "ADRIA", "Microsept", "ISHA", "Institut Pasteur de Lille", "IPL Sant",
    "ACTALIA", "Labocea", "LDA 22", "Eurofins", "CTCPA", "Campden BRI",
    "NIZO", "TNO", "Q-lip", "Qlip", "Wageningen", "RIKILT", "Fraunhofer", "SGS",
]


def encode_url(url: str) -> str:
    """Percent-encode the non-ASCII characters urllib.request refuses.

    AFNOR names a good many of its reports "N°16_..._DelvotestT.pdf", and
    urlopen() raises UnicodeEncodeError on the degree sign rather than
    escaping it. That looked exactly like an unreadable PDF in the error
    log, so it went unnoticed while silently skipping 11 of the 238
    reports on every pass -- Delvotest T among them, one of the six methods
    whose technology is still unknown.

    safe="/%" keeps an already-escaped URL intact instead of turning its
    %20 into %2520.
    """
    parts = urlsplit(url)
    return urlunsplit(parts._replace(
        path=quote(parts.path, safe="/%"),
        query=quote(parts.query, safe="=&%"),
    ))


def read_opening_text(pdf_path: Path, pages: int = PAGES_TO_READ) -> str:
    out = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages[:pages]:
            out.append(page.extract_text() or "")
    return "\n".join(out)


# A sentence-pattern capture runs on past the lab name into the rest of the
# sentence ("Nofima AS in Norway." from "...performed by Nofima AS in
# Norway."). Cut at the first connector word or sentence end so what's
# stored is the name itself.
_CAPTURE_TAIL_RE = re.compile(
    r'\s+(?:in|at|on|for|and|with|from|according|located|based|situated|'
    r'en|au|aux|dans|selon|pour|et|sur)\b.*$|[.;,].*$',
    re.I | re.S,
)


def _trim_capture(name: str) -> str:
    return _CAPTURE_TAIL_RE.sub("", name).strip()


def find_expert_lab(text: str):
    """Known laboratories are checked FIRST, sentence patterns only as a
    fallback. The reverse order (patterns first) is what the first full run
    over 238 reports actually shipped, and it produced two junk values --
    "For" from a fragment and "W. Jacobs-Reitsma", a researcher credited in
    the study rather than the lab that ran it -- on reports whose header
    named the real lab perfectly well. Matching the known name is high
    confidence; parsing an arbitrary sentence is not."""
    lowered = text.lower()
    for token in _KNOWN_LAB_TOKENS:
        if token.lower() in lowered:
            return canonical_expert_lab(token)
    for pattern in _EXPERT_LAB_PATTERNS:
        m = re.search(pattern, text)
        if m:
            # canonical_expert_lab returns None for an implausible capture,
            # so keep trying the remaining patterns rather than settling.
            lab = canonical_expert_lab(_trim_capture(m.group(1)))
            if lab:
                return lab
    return None


def process(record: dict, text: str) -> bool:
    """Fill in what this report actually evidences. Returns True if the
    record changed, so the caller only rewrites files that really moved."""
    changed = False

    lab = find_expert_lab(text)
    if lab:
        study = record.setdefault("study_design", {})
        if study.get("expert_laboratory") != lab:
            study["expert_laboratory"] = lab
            changed = True

    method_type = record.setdefault("method_type", {})
    current = method_type.get("category")
    if current in (None, "other"):
        # study_text only decides when the name gave nothing -- see
        # canonical_method_category's documented precedence.
        resolved = canonical_method_category(current, record.get("commercial_name"), text)
        if resolved and resolved != current:
            method_type["category"] = resolved
            changed = True

    return changed


# Vocabulary that hints at a detection principle without asserting one. This
# is deliberately WIDER and weaker than _STUDY_TEXT_EVIDENCE in taxonomy.py:
# its job is to show a human where in the report the principle is described,
# not to decide anything. Nothing in this list writes to a record.
_EVIDENCE_HINTS = [
    "pcr", "amplification", "primer", "probe", "dna", "rna", "nucleic",
    "thermocycler", "thermal cycler", "isothermal", "hybridi",
    "antibod", "elisa", "immuno", "conjugate", "lateral flow",
    "cytometr", "fluoresc", "luminescen", "atp", "impedance", "conductance",
    "agar", "broth", "chromogenic", "medium", "media", "colony", "incubat",
    "inhibition", "bioassay", "spore", "enzymatic", "substrate",
    "principle", "principe", "technology", "detection is based",
]


def dump_evidence(cert: str, name: str, text: str) -> None:
    """Print what the report says about its own detection principle, and
    write nothing.

    Six certificates survive every automatic rule, and the tempting move is
    to add keywords until they classify. That would be guessing dressed as
    code: a wrong technology on a validated method is worse for the reader
    than an honest "Other". So this mode exists to put the report's own
    words in front of a human first, and the rules get written from what
    comes back.
    """
    print(f"\n{'=' * 72}\n{cert} -- {name}\n{'=' * 72}")
    if not text.strip():
        # Distinguishes a scanned/image-only PDF from one that simply never
        # states its principle -- different problems, different fixes.
        print("  (no extractable text: image-only or encrypted PDF)")
        return

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hits = [ln for ln in lines if any(h in ln.lower() for h in _EVIDENCE_HINTS)]
    if hits:
        for ln in hits[:25]:
            print(f"  | {ln[:200]}")
    else:
        # No hint at all is itself a finding: show the opening lines so the
        # reader can see whether the cover page is just a title block.
        print("  (no principle vocabulary found -- first lines of the report:)")
        for ln in lines[:15]:
            print(f"  . {ln[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="data/methods")
    ap.add_argument("--only-unclassified", action="store_true",
                    help="Only process records whose detection technology is still unknown "
                         "-- a few downloads instead of every report.")
    ap.add_argument("--limit", type=int, default=0, help="Stop after N reports (0 = no cap).")
    ap.add_argument("--pages", type=int, default=PAGES_TO_READ,
                    help=f"Opening pages to read per report (default {PAGES_TO_READ}). Some "
                         "reports put the principle later -- 2015LR53's contents page lists "
                         "'2.2 Alternative method' on page 9 -- so the unclassified pass reads "
                         "deeper than the weekly full pass, which stays cheap at the default.")
    ap.add_argument("--dump-evidence", action="store_true",
                    help="Print what each report says about its detection principle and "
                         "write nothing. Use with --only-unclassified to inspect the "
                         "records no rule has resolved, before inventing a rule for them.")
    args = ap.parse_args()

    methods_dir = Path(args.methods_dir)
    targets = []
    for f in sorted(methods_dir.glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        url = (record.get("traceability") or {}).get("summary_report_pdf_url")
        if not url:
            continue
        if args.only_unclassified and (record.get("method_type") or {}).get("category") not in (None, "other"):
            continue
        targets.append((f, record, url))

    if args.limit:
        targets = targets[:args.limit]
    print(f"{len(targets)} report(s) to read (first {args.pages} pages each)", file=sys.stderr)

    written = failed = 0
    for path, record, url in targets:
        cert = record.get("source_certificate_number") or path.stem
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                with urllib.request.urlopen(encode_url(url), timeout=REQUEST_TIMEOUT) as resp:
                    tmp.write(resp.read())
                tmp.flush()
                text = read_opening_text(Path(tmp.name), pages=args.pages)
        except Exception as exc:  # noqa: BLE001 -- one unreadable report must not abort the pass
            print(f"[{cert}] ERROR: {exc}", file=sys.stderr)
            failed += 1
            continue

        if args.dump_evidence:
            dump_evidence(cert, record.get("commercial_name") or "?", text)
            continue

        if process(record, text):
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            lab = (record.get("study_design") or {}).get("expert_laboratory")
            cat = (record.get("method_type") or {}).get("category")
            print(f"[{cert}] lab={lab!r} category={cat!r}", file=sys.stderr)
            written += 1

    if args.dump_evidence:
        print(f"\n=== {len(targets) - failed} report(s) dumped, {failed} unreadable; "
              f"nothing written ===", file=sys.stderr)
    else:
        print(f"\n=== {written} record(s) updated, {failed} unreadable, "
              f"{len(targets) - written - failed} unchanged ===", file=sys.stderr)


if __name__ == "__main__":
    main()
