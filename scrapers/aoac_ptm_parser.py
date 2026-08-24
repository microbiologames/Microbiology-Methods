"""Parse AOAC Research Institute Performance Tested Methods (PTM) certificates.

Unlike NF-Validation (certificate + separate summary report) AOAC bundles
certification metadata AND validation-study performance data (inclusivity/
exclusivity, POD comparison tables) into a single PDF per certificate.

Two template generations are in circulation among the 4 example certificates
this parser was built and tested against:
  - "long form" (pre-2022ish): AUTHORS / SUBMITTING COMPANY / INDEPENDENT
    LABORATORY / APPLICABILITY OF METHOD / narrative DISCUSSION OF THE
    VALIDATION STUDY section that states inclusivity/exclusivity counts in
    prose (e.g. "Of the 50 inclusivity strains ... all 50 ... correctly
    detected").
  - "compact form" (newer, e.g. cert 022203): CERTIFIED CLAIM STATEMENT +
    a "Method selectivity" table giving inclusivity/exclusivity as a clean
    No.-tested/No.-positive pair -- no per-strain table needed.

Field-level coverage differs between the two templates (the compact form has
no free-text "Target organisms"/"Matrixes" statement, for instance) -- fields
this parser cannot find are left null rather than guessed, consistent with
the project's schema convention.

Only 4 real AOAC certificates have been available to develop this against, so
treat this as a first-pass heuristic parser to be refined against more
examples once a normal-egress environment can pull further certificates from
members.aoac.org.

Usage (offline, from saved certificate PDFs):
    python3 aoac_ptm_parser.py --pdf-dir DIR --out-dir data/aoac_ptm
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pypdf

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
DATE_RE = re.compile(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})')


def parse_date(text: str):
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"


# Ordered so that, for a given PDF, whichever of these headers actually
# appear can be sliced against "whichever comes next" regardless of template.
SECTION_HEADERS = [
    ("authors", r'\bAUTHORS\b'),
    ("submitting_company", r'\bSUBMITTING COMPANY\b'),
    ("current_sponsor", r'\bCURRENT SPONSOR\b'),
    ("method_name_hdr", r'\bMETHOD NAME\b'),
    ("catalog_numbers", r'\bCATALOG NUMBERS?\b'),
    ("independent_laboratory", r'\bINDEPENDENT LABORATORY\b'),
    ("applicability", r'\bAPPLICABILITY OF METHOD\b'),
    ("certified_claim_statement", r'\bCERTIFIED CLAIM STATEMENT\b'),
    ("reference_method", r'\bREFERENCE METHODS? AND GUIDELINES\b|\bREFERENCE METHODS?\b'),
    ("original_certification_date", r'\bORIGINAL CERTIFICATION DATE\b'),
    ("certification_renewal_record", r'\bCERTIFICATION RENEWAL RECORD\b'),
    ("method_modification_record", r'\bMETHOD MODIFICATION RECORD\b'),
    ("summary_of_modification", r'\bSUMMARY OF MODIFICATION\b'),
    ("distributed_by", r'this method is distributed by:?'),
    ("distributed_as", r'this method is distributed as:?'),
    ("principle", r'\bPRINCIPLE OF THE METHOD\b'),
    ("discussion", r'\bDISCUSSION OF THE VALIDATION STUDY\b'),
    ("method_selectivity_table", r'Table\s*\d+\.\s*Method selectivity'),
    ("method_history_table", r'Table\s*\d+\.\s*Method history'),
    ("references_cited", r'\bREFERENCES CITED\b'),
]


def slice_sections(full_text: str) -> dict:
    matches = []
    for key, pattern in SECTION_HEADERS:
        m = re.search(pattern, full_text, re.I)
        if m:
            matches.append((m.start(), m.end(), key))
    matches.sort()
    sections = {}
    for i, (start, end, key) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(full_text)
        sections[key] = re.sub(r'\s+', ' ', full_text[end:next_start]).strip()
    return sections


def extract_header_fields(full_text: str) -> dict:
    cert_m = re.search(r'Certificate No\.?\s*\n?\s*(\d+)', full_text)
    name_m = re.search(
        r'hereby certifies the method known as\s*(.*?)\s*manufactured by',
        full_text, re.S,
    )
    mfr_m = re.search(
        r'manufactured by\s*(.*?)\s*This method has been evaluated',
        full_text, re.S,
    )
    issue_m = re.search(r'Issue Date\s+([A-Za-z]+ \d{1,2},?\s*\d{4})', full_text)
    expiry_m = re.search(r'Expiration Date\s+([A-Za-z]+ \d{1,2},?\s*\d{4})', full_text)

    manufacturer_raw = re.sub(r'\s+', ' ', mfr_m.group(1)).strip() if mfr_m else None
    manufacturer_name = manufacturer_raw.split(",")[0].strip() if manufacturer_raw else None
    # First line only is the company name; the rest is address.
    manufacturer_address = None
    if mfr_m:
        lines = [l.strip() for l in mfr_m.group(1).strip().split("\n") if l.strip()]
        if lines:
            manufacturer_name = lines[0]
            manufacturer_address = ", ".join(lines[1:]) if len(lines) > 1 else None

    return {
        "certificate_number": cert_m.group(1) if cert_m else None,
        "method_name": re.sub(r'\s+', ' ', name_m.group(1)).strip() if name_m else None,
        "manufacturer_raw": manufacturer_raw,
        "manufacturer_name": manufacturer_name,
        "manufacturer_address_raw": manufacturer_address,
        "issue_date": parse_date(issue_m.group(1)) if issue_m else None,
        "expiration_date": parse_date(expiry_m.group(1)) if expiry_m else None,
    }


TARGET_ORGANISM_KEYWORDS = [
    ("SALMONELLA", "Salmonella spp."),
    ("LISTERIA MONOCYTOGENES", "Listeria monocytogenes"),
    ("LISTERIA", "Listeria spp."),
    ("CRONOBACTER", "Cronobacter spp."),
    ("CAMPYLOBACTER", "Campylobacter spp."),
    ("SHIGA TOXIN", "Shiga toxin-producing E. coli (STEC)"),
    ("STEC", "Shiga toxin-producing E. coli (STEC)"),
    ("EHEC", "Shiga toxin-producing E. coli (STEC)"),
    ("PATHOGENIC E. COLI", "Shiga toxin-producing E. coli (STEC)"),
    ("E. COLI O157", "E. coli O157"),
    ("E. COLI", "E. coli"),
    ("COLIFORM", "Coliforms"),
    ("ENTEROBACTERIACEAE", "Enterobacteriaceae"),
    ("STAPHYLOCOCC", "Coagulase-positive staphylococci"),
    ("BACILLUS CEREUS", "Bacillus cereus"),
    ("YEAST AND MOLD", "Yeasts and molds"),
    ("PSEUDOMONAS", "Pseudomonas spp."),
    ("ANTIBIOTIC", "Antibiotic residues"),
]


def normalize_target_organism(*texts):
    combined = " ".join(t for t in texts if t).upper()
    for kw, label in TARGET_ORGANISM_KEYWORDS:
        if kw in combined:
            return label
    return None


def guess_category(*texts):
    combined = " ".join(t for t in texts if t).lower()
    if re.search(r'\breal[- ]?time pcr\b|\bqpcr\b|\bpcr\b|amplification', combined):
        return "molecular_pcr"
    if re.search(r'chromogenic|colou?r change', combined):
        return "chromogenic_agar"
    if re.search(r'elisa|immunoassay|lateral flow|immuno-?concentrat', combined):
        return "immunological_elisa"
    return "other"


def extract_applicability(applicability_text: str):
    if not applicability_text:
        return None, None
    target_m = re.search(
        r'(?:Target [Oo]rganisms?|Analyte)\s*[-–]\s*(.*?)(?:Matrixe?s?\s*[-–]|Performance claims|$)',
        applicability_text, re.S,
    )
    matrix_m = re.search(
        r'Matrixe?s?\s*[-–]\s*(.*?)(?:Performance claims|$)',
        applicability_text, re.S,
    )
    target_raw = target_m.group(1).strip().rstrip(".") if target_m else None
    matrices_raw = matrix_m.group(1).strip().rstrip(".") if matrix_m else None
    return target_raw, matrices_raw


def split_matrices(matrices_raw: str):
    if not matrices_raw:
        return []
    parts = re.split(r',|\band\b', matrices_raw)
    return [re.sub(r'\s+', ' ', p).strip(" .") for p in parts if re.sub(r'\s+', ' ', p).strip(" .")]


def extract_strain_table_rows(full_text: str, table_label_pattern: str):
    """Fallback for certificates whose narrative discussion doesn't state
    inclusivity/exclusivity counts in the "Of the N strains..." phrasing
    (e.g. InSite Listeria): read numbered strain rows directly from the
    per-strain table, each of which ends in a +/- result symbol -- often
    with a footnote letter attached (e.g. '+c') marking a noteworthy
    exception, which must NOT be dropped just because it isn't a bare
    '+'/'-' (that would silently hide real cross-reactivity findings).
    Returns a list of (strain_label, symbol) or [] if no such table found.
    A row with no result symbol at all (a genuine gap in the source PDF) is
    skipped, since there is nothing to report for it.
    """
    m = re.search(table_label_pattern, full_text, re.I)
    if not m:
        return []
    # Table runs until the next "Table N." or end of document.
    end_m = re.search(r'\bTable\s*\d+\.', full_text[m.end():])
    table_text = full_text[m.end(): m.end() + end_m.start()] if end_m else full_text[m.end():]

    row_re = re.compile(r'^\s*\d+\s+(.*?)\s([+\-])[a-z]{0,2}\s*$', re.M)
    return [(re.sub(r'\s+', ' ', label).strip(), symbol) for label, symbol in row_re.findall(table_text)]


def extract_inclusivity_exclusivity(full_text: str, sections: dict):
    """Three independent strategies, tried in order:
    1. Narrative prose in the DISCUSSION section ("Of the N inclusivity
       strains ... all/none N were correctly detected").
    2. A compact "Method selectivity" table giving aggregate No. tested /
       No. positive pairs directly.
    3. Counting +/- rows directly in the per-strain inclusivity/exclusivity
       tables, for certificates whose narrative doesn't follow pattern 1.
    Returns (inclusivity_dict, exclusivity_dict, source_text) -- any piece
    not found stays None so we never fabricate a count.
    """
    discussion = sections.get("discussion", "")

    incl = {"n_tested": None, "n_correctly_detected": None, "discrepancies": []}
    excl = {"n_tested": None, "n_correctly_detected": None, "discrepancies": []}
    source_text = None

    incl_m = re.search(
        r'Of the (\d+) inclusivity strains.*?(all (\d+)|none)[^.]*correctly detected',
        discussion, re.I,
    )
    if incl_m:
        n_tested = int(incl_m.group(1))
        n_correct = int(incl_m.group(3)) if incl_m.group(3) else 0
        incl = {"n_tested": n_tested, "n_correctly_detected": n_correct, "discrepancies": []}

    excl_m = re.search(
        r'Of the (\d+) exclusivity strains,?\s*(none|all (\d+))\s*(?:were|was)?\s*detected',
        discussion, re.I,
    )
    if excl_m:
        n_tested = int(excl_m.group(1))
        # For exclusivity, "correctly detected" means correctly NOT detected by the assay.
        n_correct = n_tested if excl_m.group(2).lower() == "none" else (n_tested - int(excl_m.group(3)))
        excl = {"n_tested": n_tested, "n_correctly_detected": n_correct, "discrepancies": []}

    if incl_m or excl_m:
        source_text = discussion[:600]

    # Narrative counts and per-strain table counts are complementary, not
    # mutually exclusive: even when the narrative gives clean totals, the
    # per-strain table is the only place discrepancies (cross-reactivity,
    # missed strains) get recorded, so always look for it.
    incl_rows = extract_strain_table_rows(full_text, r'Table\s*\d+\.\s*Inclusivity')
    if incl_rows:
        discrepancies = [{"strain": label, "note": f"Not detected (result: {sym})"}
                          for label, sym in incl_rows if not sym.startswith("+")]
        if incl["n_tested"] is None:
            incl = {
                "n_tested": len(incl_rows),
                "n_correctly_detected": sum(1 for _, s in incl_rows if s.startswith("+")),
                "discrepancies": discrepancies,
            }
        else:
            incl["discrepancies"] = discrepancies

    excl_rows = extract_strain_table_rows(full_text, r'Table\s*\d+\.\s*Exclusivity')
    if excl_rows:
        discrepancies = [{"strain": label, "note": f"Cross-reactivity: detected (result: {sym})"}
                          for label, sym in excl_rows if not sym.startswith("-")]
        if excl["n_tested"] is None:
            excl = {
                "n_tested": len(excl_rows),
                "n_correctly_detected": sum(1 for _, s in excl_rows if s.startswith("-")),
                "discrepancies": discrepancies,
            }
        else:
            excl["discrepancies"] = discrepancies

    if incl["n_tested"] is None and excl["n_tested"] is None:
        # Fall back to the compact "Method selectivity" table: a row of
        # "<broth> <temp> <n_tested_incl><letter?> <n_pos_incl> <n_tested_excl><letter?> <n_pos_excl>"
        sel_text = sections.get("method_selectivity_table", "")
        row_m = re.search(
            r'(\d+)[a-z]?\s+(\d+)\s+(\d+)[a-z]?\s+(\d+)\b', sel_text,
        )
        if row_m:
            incl = {"n_tested": int(row_m.group(1)), "n_correctly_detected": int(row_m.group(2)), "discrepancies": []}
            excl_tested = int(row_m.group(3))
            excl_positive = int(row_m.group(4))
            excl = {"n_tested": excl_tested, "n_correctly_detected": excl_tested - excl_positive, "discrepancies": []}
            source_text = sel_text[:400]

    if not source_text and (incl_rows or excl_rows):
        source_text = "Counted from per-strain inclusivity/exclusivity table rows."

    return incl, excl, source_text


def parse_certificate(pdf_path: Path) -> dict:
    reader = pypdf.PdfReader(str(pdf_path))
    pages_text = [p.extract_text() or "" for p in reader.pages]
    full_text = "\n".join(pages_text)

    header_fields = extract_header_fields(full_text)
    sections = slice_sections(full_text)

    target_raw, matrices_raw = extract_applicability(sections.get("applicability", ""))
    if not target_raw:
        # Compact template: no "Target organisms -" line; fall back to the
        # certified claim statement / method name for keyword matching only
        # (kept separate from target_raw, which stays null -- we don't want
        # to fabricate a "raw" quote that wasn't actually printed as such).
        pass

    target_organism = normalize_target_organism(
        target_raw, sections.get("certified_claim_statement"), header_fields["method_name"],
    )

    matrices = split_matrices(matrices_raw)
    if not matrices:
        # Compact template: matrices appear as a table column instead of prose.
        matrix_col_matches = re.findall(
            r'\n([A-Z][A-Za-z0-9 ()%.\-]{3,40}) \d+ (?:g|mL|cloth)\b', full_text,
        )
        matrices = sorted(set(m.strip() for m in matrix_col_matches))

    inclusivity, exclusivity, incl_excl_source = extract_inclusivity_exclusivity(full_text, sections)

    original_cert_date = parse_date(sections.get("original_certification_date"))

    category = guess_category(
        sections.get("principle"), header_fields["method_name"], sections.get("certified_claim_statement"),
    )

    return {
        "source": "AOAC-RI",
        **header_fields,
        "authors_raw": sections.get("authors") or None,
        "submitting_company_raw": sections.get("submitting_company") or None,
        "independent_laboratory_raw": sections.get("independent_laboratory") or None,
        "catalog_numbers_raw": sections.get("catalog_numbers") or None,
        "target_organism_raw": target_raw,
        "target_organism": target_organism,
        "category_guess": category,
        "matrices_raw": matrices_raw,
        "matrices": matrices,
        "reference_method_raw": sections.get("reference_method") or None,
        "original_certification_date": original_cert_date,
        "certification_renewal_record_raw": sections.get("certification_renewal_record") or None,
        "inclusivity": inclusivity,
        "exclusivity": exclusivity,
        "inclusivity_exclusivity_source_text": incl_excl_source,
        "source_pdf_filename": pdf_path.name,
        "provenance": {
            "source_type": "manually_supplied_certificate_pdf",
            "note": (
                "Parsed from a user-supplied AOAC Performance Tested Methods certificate PDF. "
                "members.aoac.org (the live index) was unreachable from this environment "
                "(egress-blocked); this parser has only been developed/tested against 4 "
                "example certificates and field coverage will need broadening once more "
                "certificates are available."
            ),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out-dir", default="data/aoac_ptm")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        rec = parse_certificate(pdf_path)
        records.append(rec)
        fname = re.sub(r'[^A-Za-z0-9]+', '_', rec["certificate_number"] or pdf_path.stem).strip('_') + ".json"
        (out_dir / fname).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{pdf_path.name}: cert {rec['certificate_number']} -> {fname}", file=sys.stderr)

    print(f"Parsed {len(records)} AOAC-RI certificates -> {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
