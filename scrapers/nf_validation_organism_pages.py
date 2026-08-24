"""Parse AFNOR NF-Validation "per target organism" pages
(https://nf-validation.afnor.org/domaine-agroalimentaire/<organism-slug>/).

Each page lists every certified method for one target organism, grouped by
method category (Milieux de culture / Méthodes moléculaires / Tests
immunologiques / Cytométrie en flux), with a direct link to the certificate
PDF and, usually, the summary validation report PDF.

This is the primary live-site source for NF-Validation: richer than the
certificate-list PDF (it links straight to the summary report that carries
the performance data), and each page states its own last-updated date so
staleness is directly observable.

Usage (offline, from saved pages):
    python3 nf_validation_organism_pages.py --html-dir DIR --out-dir data/nf_validation

A follow-up `--fetch-live` mode (not implemented here -- this environment has
no outbound web access) should crawl https://nf-validation.afnor.org/domaine-agroalimentaire/
for the current set of organism-page URLs and feed each page's HTML into
`parse_organism_page` unchanged.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

CATEGORY_MAP = {
    "milieux de culture": "culture_media",
    "méthodes moléculaires": "molecular_pcr",
    "methodes moleculaires": "molecular_pcr",
    "tests de biologie moléculaire": "molecular_pcr",
    "tests immunologiques": "immunological_elisa",
    "tests immuno-enzymatiques": "immunological_elisa",
    "tests imuno-enzymatiques": "immunological_elisa",  # typo present on the live site
    "cytométrie en flux": "flow_cytometry",
    "cytometrie en flux": "flow_cytometry",
}

# Organism URL slug -> normalized (English) target organism label.
# Mirrors the vocabulary already used in nf_validation_list_parser.py so
# records from both parsers merge cleanly.
ORGANISM_SLUG_MAP = {
    "salmonella": "Salmonella spp.",
    "listeria-monocytogenes": "Listeria monocytogenes",
    "listeria-spp": "Listeria spp.",
    "listeria-spp-et-listeria-monocytogenes": "Listeria spp. and L. monocytogenes",
    "coliformes": "Coliforms",
    "enterobacteriaceae": "Enterobacteriaceae",
    "e-coli": "E. coli",
    "e-coli-o157": "E. coli O157",
    "stec": "Shiga toxin-producing E. coli (STEC)",
    "cronobacter-spp-enterobacter-sakazakii": "Cronobacter spp.",
    "campylobacter-spp": "Campylobacter spp.",
    "staphylocoques-coagulase-positive": "Coagulase-positive staphylococci",
    "bacillus-cereus": "Bacillus cereus",
    "levures-moisissures": "Yeasts and molds",
    "pseudomonas-spp": "Pseudomonas spp.",
    "flore-totale": "Aerobic mesophilic flora / total viable count",
    "bacteries-lactiques-mesophiles": "Lactic acid bacteria",
    "antibiotiques": "Antibiotic residues",
}

AIM_ACTION_RE = re.compile(r'D[EÉ]TECTION|D[EÉ]NOMBREMENT|DENOMBREMENT', re.I)
REFERENCE_IN_AIM_RE = re.compile(r'\(selon\s+([^)]+)\)', re.I)
UPDATED_DATE_RE = re.compile(r'mise à jour et publiée le\s+(.+)', re.I)


def slug_from_filename_or_url(source_name: str) -> str:
    """Best-effort slug: real crawl gives a URL; offline fixtures give a
    downloaded filename like '87e36a5a-Coliformes__NF_Validation.htm'."""
    if source_name.startswith("http"):
        path = urlparse(source_name).path.strip("/")
        return path.rsplit("/", 1)[-1]
    # strip upload hash prefix + suffix, keep something url-slug-ish as fallback
    stem = Path(source_name).stem
    stem = re.sub(r'^[0-9a-f]{8}-', '', stem)
    stem = stem.replace("__NF_Validation", "")
    return re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')


def parse_method_block(p_tag, organism_reference_methods, organism_normalized, organism_raw,
                        category, source_page_url):
    lines = [re.sub(r'\s+', ' ', l).strip() for l in p_tag.get_text().split("\n")]
    lines = [l for l in lines if l]
    if not lines:
        return None
    commercial_name = lines[0]

    manufacturer = None
    aim_raw = None
    for line in lines[1:]:
        if line.lower().startswith("titulaire") or line.lower().startswith("société"):
            manufacturer = line.split(":", 1)[-1].strip().lstrip(":").strip()
        elif AIM_ACTION_RE.search(line) and aim_raw is None:
            aim_raw = line

    action = None
    if aim_raw:
        if re.search(r'D[EÉ]TECTION', aim_raw, re.I):
            action = "detection"
        elif re.search(r'D[EÉ]NOMBREMENT|DENOMBREMENT', aim_raw, re.I):
            action = "enumeration"

    reference_standard = None
    if aim_raw:
        m = REFERENCE_IN_AIM_RE.search(aim_raw)
        if m:
            reference_standard = m.group(1).strip()
    if not reference_standard and organism_reference_methods:
        reference_standard = "; ".join(organism_reference_methods)

    certificate_url = None
    summary_report_url = None
    for a in p_tag.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if "certificat" in text or "attestation" in text:
            certificate_url = a["href"]
        elif "synth" in text or "rapport" in text:
            summary_report_url = a["href"]

    # Filter out non-method paragraphs (nav links like "Retour à la liste",
    # stray blurbs, etc.) -- a genuine method block always names a manufacturer
    # and/or links a document.
    if not (manufacturer or certificate_url or summary_report_url):
        return None

    return {
        "source": "NF-VALIDATION",
        "commercial_name": commercial_name,
        "manufacturer": {"name": manufacturer, "address_raw": None,
                          "represented_in_europe": None, "site_of_production": None},
        "method_type": {"action": action, "category": category},
        "target_organism": {"normalized": organism_normalized, "raw": organism_raw},
        "reference_method": {"standard": reference_standard, "raw": aim_raw},
        "traceability": {
            "source_document_type": "live_web_page",
            "source_document_url": source_page_url,
            "certificate_pdf_url": certificate_url,
            "summary_report_pdf_url": summary_report_url,
            "extraction_confidence": "high" if manufacturer and aim_raw else "medium",
        },
    }


def parse_organism_page(html: str, source_page_url: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    h1 = soup.find("h1")
    organism_raw = re.sub(r'\s+', ' ', h1.get_text(" ", strip=True)) if h1 else None

    slug = slug_from_filename_or_url(source_page_url)
    organism_normalized = ORGANISM_SLUG_MAP.get(slug, organism_raw)

    # Reference method standards listed near the top of the page (before the
    # first h2), e.g. links to NF ISO 4831 / NF ISO 4832 / NF V08-060.
    organism_reference_methods = []
    first_h2 = soup.find("h2")
    if first_h2:
        for a in first_h2.find_all_previous("a", href=True):
            if "boutique.afnor.org" in a["href"] or re.match(r'NF\s|ISO\s|EN\s', a.get_text(strip=True)):
                organism_reference_methods.append(a.get_text(strip=True))
        organism_reference_methods.reverse()

    last_updated = None
    updated_match = UPDATED_DATE_RE.search(soup.get_text())
    if updated_match:
        last_updated = updated_match.group(1).strip()

    records = []
    for h2 in soup.find_all("h2"):
        category_label = h2.get_text(strip=True).lower()
        category = CATEGORY_MAP.get(category_label, "other")
        for sib in h2.find_next_siblings():
            if sib.name == "h2":
                break
            if sib.name != "p":
                continue
            rec = parse_method_block(
                sib, organism_reference_methods, organism_normalized, organism_raw,
                category, source_page_url,
            )
            if rec:
                rec["organism_page_last_updated_raw"] = last_updated
                records.append(rec)
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html-dir", required=True,
                     help="Directory of saved organism-page HTML files (offline mode).")
    ap.add_argument("--out-dir", default="data/nf_validation_organism_pages")
    args = ap.parse_args()

    html_dir = Path(args.html_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_records = []
    files = sorted(html_dir.glob("*.htm*"))
    for fpath in files:
        if "domaine_agroalimentaire" in fpath.name.lower() or "domaine agroalimentaire" in fpath.name.lower():
            continue  # index page, not an organism page
        html = fpath.read_text(encoding="utf-8", errors="ignore")
        slug = slug_from_filename_or_url(str(fpath))
        canonical_url = f"https://nf-validation.afnor.org/domaine-agroalimentaire/{slug}/"
        records = parse_organism_page(html, canonical_url)
        all_records.extend(records)
        print(f"{fpath.name}: {len(records)} methods", file=sys.stderr)

    for i, rec in enumerate(all_records):
        org_slug = re.sub(r'[^a-z0-9]+', '-', (rec["target_organism"]["normalized"] or "unknown").lower()).strip('-')
        name_slug = re.sub(r'[^a-z0-9]+', '-', rec["commercial_name"].lower()).strip('-')[:40]
        fname = f"{org_slug}--{name_slug}--{i}.json"
        with open(out_dir / fname, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)

    print(f"Total: {len(all_records)} methods -> {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
