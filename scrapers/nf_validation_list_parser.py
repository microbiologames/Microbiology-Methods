"""Parse the AFNOR NF-Validation "list of certified methods" PDF into structured JSON.

Input: the official NF-Validation food-application PDF list (one row per certified
method, grouped by company holder of certification).
Output: one JSON file per certificate under data/nf_validation/, plus an index.

This is a heuristic text-layout parser (no site access required) — the source PDF
is downloaded by hand today and passed as CLI arg; a future GitHub Actions job can
fetch the PDF from nf-validation.afnor.org and re-run this unchanged.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pypdf

CERT_START_RE = re.compile(
    r'^([A-Z0-9]{2,4} \d{2}/\d{2}-\d{2}/\d{2}(?: [A-Z])?)\b', re.M
)
DATE_RE = re.compile(r'\b(\d{2})[./](\d{2})[./](\d{4})\b')
AIM_KEYWORDS = re.compile(
    r'\b(D[EÉ]TECTION|DENOMBREMENT|D[EÉ]NOMBREMENT|RECHERCHE)\b', re.I
)
COMPANY_HEADER_RE = re.compile(
    r"(?:Soci[ée]t[ée] [Tt]itulaire de la certification|Company holder of the certification)"
    r"(.*?)"
    r"N[°o] certificat",
    re.S,
)

TARGET_ORGANISM_KEYWORDS = [
    ("SALMONELLA", "Salmonella spp."),
    ("LISTERIA MONOCYTOGENES", "Listeria monocytogenes"),
    ("LISTERIA", "Listeria spp."),
    ("CRONOBACTER", "Cronobacter spp."),
    ("CAMPYLOBACTER", "Campylobacter spp."),
    ("STEC", "Shiga toxin-producing E. coli (STEC)"),
    ("E. COLI O157", "E. coli O157"),
    ("E. COLI", "E. coli"),
    ("COLIFORM", "Coliforms"),
    ("ENTEROBACTERIACEAE", "Enterobacteriaceae"),
    ("STAPHYLOCOCC", "Coagulase-positive staphylococci"),
    ("BACILLUS CEREUS", "Bacillus cereus"),
    ("YEAST AND MOLD", "Yeasts and molds"),
    ("PSEUDOMONAS", "Pseudomonas spp."),
    ("ANTIBIOTIC", "Antibiotic residues"),
    ("AEROBIC", "Aerobic mesophilic flora / total viable count"),
    ("LACTIC ACID BACTERIA", "Lactic acid bacteria"),
]


def extract_target_organism(aim_text: str):
    upper = aim_text.upper()
    for kw, label in TARGET_ORGANISM_KEYWORDS:
        if kw in upper:
            return label
    return None


def extract_action(aim_text: str):
    upper = aim_text.upper()
    if re.search(r'D[EÉ]TECTION', upper):
        return "detection"
    if re.search(r'D[EÉ]NOMBREMENT|DENOMBREMENT|ENUMERATION', upper):
        return "enumeration"
    return None


def parse_company_block(page_text: str):
    m = COMPANY_HEADER_RE.search(page_text)
    if not m:
        return {"raw": None}
    block = m.group(1).strip()
    # Split on the "Represented in Europe" / "Site of production" sub-headers, keep raw.
    return {"raw": re.sub(r'\s+', ' ', block)[:600]}


def parse_certificate_block(cert_id: str, block_text: str):
    block_text = block_text.strip()
    body = block_text[len(cert_id):].strip() if block_text.startswith(cert_id) else block_text

    aim_match = AIM_KEYWORDS.search(body)
    commercial_name = body[: aim_match.start()].strip() if aim_match else None
    commercial_name = re.sub(r'\s+', ' ', commercial_name) if commercial_name else None

    dates = DATE_RE.findall(block_text)
    dates_iso = [f"{y}-{mo}-{d}" for d, mo, y in dates]
    certification_date = dates_iso[0] if dates_iso else None
    end_of_validity = dates_iso[-1] if dates_iso else None

    aim_section = ""
    scope_section = ""
    if aim_match:
        rest = body[aim_match.start():]
        # crude split: aim section ends, scope begins at first '*' bullet or ALL/TOUS caps run
        scope_match = re.search(r'(\*?TOUS PRODUITS|\*?ALL HUMAN|\*?LES |\*?RAW |\*?VIANDE)', rest)
        if scope_match:
            aim_section = rest[: scope_match.start()].strip()
            after_scope = rest[scope_match.start():]
            date_start = DATE_RE.search(after_scope)
            scope_section = after_scope[: date_start.start()].strip() if date_start else after_scope.strip()
        else:
            aim_section = rest.strip()

    return {
        "certificate_number": cert_id,
        "source": "NF-VALIDATION",
        "commercial_name": commercial_name,
        "aim_of_method_raw": re.sub(r'\s+', ' ', aim_section)[:500] if aim_section else None,
        "target_organism": extract_target_organism(aim_section or block_text),
        "action": extract_action(aim_section or block_text),
        "validation_scope_raw": re.sub(r'\s+', ' ', scope_section)[:1000] if scope_section else None,
        "certification_date": certification_date,
        "end_of_validity": end_of_validity,
        "all_dates_found": dates_iso,
        "raw_block": re.sub(r'\s+', ' ', block_text)[:2000],
        "provenance": {
            "source_type": "bootstrap_pdf_import",
            "note": (
                "Imported from a manually-supplied AFNOR NF-Validation list PDF. "
                "This PDF's own refresh cadence is unknown and is NOT guaranteed to "
                "match the live nf-validation.afnor.org site. Treat as a one-time "
                "seed, not a recurring source -- the production scraper must read "
                "the live site directly and this record should be superseded/"
                "reconciled once that scraper runs."
            ),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_path")
    ap.add_argument("--out-dir", default="data/nf_validation")
    ap.add_argument("--index-page-end", type=int, default=77,
                     help="last page (1-indexed) that still contains certificate rows, "
                          "before the target-organism cross-reference tables begin")
    args = ap.parse_args()

    reader = pypdf.PdfReader(args.pdf_path)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    first_page_text = reader.pages[0].extract_text() or ""
    list_date_match = re.search(r'[Ll]ist valid on\s*:?\s*(\d{2})[.\-/](\d{2})[.\-/](\d{4})', first_page_text)
    pdf_list_date = (
        f"{list_date_match.group(3)}-{list_date_match.group(2)}-{list_date_match.group(1)}"
        if list_date_match else None
    )

    records = []
    seen_ids = set()
    for page_num, page in enumerate(reader.pages[: args.index_page_end], start=1):
        text = page.extract_text() or ""
        company = parse_company_block(text)

        anchors = list(CERT_START_RE.finditer(text))
        if not anchors:
            continue
        for i, m in enumerate(anchors):
            cert_id = m.group(1).strip()
            start = m.start()
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
            block_text = text[start:end]
            rec = parse_certificate_block(cert_id, block_text)
            rec["company_holder_raw"] = company["raw"]
            rec["source_page"] = page_num
            rec["provenance"]["pdf_list_valid_on"] = pdf_list_date
            dedup_key = cert_id
            if dedup_key in seen_ids:
                continue
            seen_ids.add(dedup_key)
            records.append(rec)

    for rec in records:
        fname = re.sub(r'[^A-Za-z0-9]+', '_', rec["certificate_number"]).strip('_')
        with open(out_dir / f"{fname}.json", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)

    with open(out_dir / "_index.json", "w", encoding="utf-8") as f:
        json.dump(
            {"source": "NF-VALIDATION", "count": len(records),
             "certificate_numbers": [r["certificate_number"] for r in records]},
            f, ensure_ascii=False, indent=2,
        )

    print(f"Parsed {len(records)} certificates -> {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
