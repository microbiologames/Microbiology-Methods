"""Merge the two NF-Validation collectors into canonical method records.

Inputs (two independent collectors of the SAME certifying body):
  - data/nf_validation/                 bootstrap PDF-list import
      rich date history (original/renewal/extension), full validation-scope
      text, but noisy commercial_name and no document links.
  - data/nf_validation_organism_pages/  live organism-page scrape
      clean manufacturer/action/category/target-organism/reference-standard,
      direct certificate + summary-report PDF URLs, but no scope text and no
      renewal history (only the current record as published today).

Join key: the certificate number, which the bootstrap collector reads
directly off the list PDF and which this script re-derives from the
organism-page collector's certificate_pdf_url filename (both follow AFNOR's
"PREFIX-NN-NN-NN-NN[-LETTER]" naming convention).

Output: one canonical record per certificate under data/methods/, validated
against schema/method.schema.json.
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import jsonschema

CERT_FROM_FILENAME_RE = re.compile(
    r'([A-Z0-9]{2,4})[_-](\d{2})[_-](\d{2})[_-](\d{2})[_-](\d{2})(?:[_-]([A-Z])(?=[_.\-]|$))?'
)


def cert_number_from_pdf_url(url: str) -> str | None:
    """'.../BKR-23-07-10-11_fr.pdf' -> 'BKR 23/07-10/11'
    '.../3M-01-02-09-89-A_fr.docx-2.pdf' -> '3M 01/02-09/89 A'"""
    if not url:
        return None
    filename = url.rsplit("/", 1)[-1]
    m = CERT_FROM_FILENAME_RE.search(filename)
    if not m:
        return None
    prefix, a, b, c, d, letter = m.groups()
    cert = f"{prefix} {a}/{b}-{c}/{d}"
    if letter:
        cert += f" {letter}"
    return cert


def split_renewal_extension_dates(raw_block: str):
    """The bootstrap parser only kept a flat all_dates_found list; re-derive
    which dates were under RECONDUCTION/RENEWAL vs EXTENSION from raw_block."""
    renewal_section = re.search(
        r'RECONDUCTION/RENEWAL:\s*(.*?)(?:EXTENSION:|$)', raw_block, re.S
    )
    extension_section = re.search(
        r'EXTENSION:\s*(.*?)(?:RECONDUCTION/RENEWAL:|$)', raw_block, re.S
    )
    date_re = re.compile(r'(\d{2})[./](\d{2})[./](\d{4})')

    def dates_in(section_text):
        if not section_text:
            return []
        return [f"{y}-{mo}-{d}" for d, mo, y in date_re.findall(section_text)]

    renewals = dates_in(renewal_section.group(1) if renewal_section else None)
    extensions = dates_in(extension_section.group(1) if extension_section else None)
    return renewals, extensions


def compute_status(current_expiry: str | None, today: date) -> str:
    if not current_expiry:
        return "unknown"
    try:
        y, m, d = (int(x) for x in current_expiry.split("-"))
        return "active" if date(y, m, d) >= today else "expired"
    except ValueError:
        return "unknown"


def load_bootstrap(bootstrap_dir: Path) -> dict:
    by_cert = {}
    for f in bootstrap_dir.glob("*.json"):
        if f.name == "_index.json":
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        by_cert[rec["certificate_number"]] = rec
    return by_cert


def load_organism_pages(organism_dir: Path) -> list:
    """Returns a flat list of (derived_cert_number, record) -- NOT yet grouped,
    since the derived cert number is only a best guess (see
    reconcile_organism_pages)."""
    out = []
    for f in organism_dir.glob("*.json"):
        rec = json.loads(f.read_text(encoding="utf-8"))
        cert = cert_number_from_pdf_url(rec["traceability"].get("certificate_pdf_url"))
        out.append((cert, rec))
    return out


def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.upper()
    name = re.sub(r'[®™©®™©]', '', name)
    name = re.sub(r'[^A-Z0-9]+', '', name)
    return name


def reconcile_organism_pages(organism_records: list, bootstrap_by_cert: dict) -> dict:
    """Group organism-page records by the certificate they actually describe.

    The certificate number derived from the PDF filename is usually right,
    but AFNOR sometimes re-uploads a certificate PDF under a filename
    date-stamped with the reissue date rather than the original certificate
    number (observed e.g. for 'BIO 12/43-02/04' TEMPO CAM, whose current
    file is 'BIO-12-43-04-20_fr.pdf'). When the derived number doesn't match
    any bootstrap record, fall back to matching on normalized commercial
    name against the still-unclaimed bootstrap records.
    """
    unclaimed_bootstrap_by_name = {}
    for cert, rec in bootstrap_by_cert.items():
        unclaimed_bootstrap_by_name.setdefault(normalize_name(rec["commercial_name"]), []).append(cert)

    by_cert = {}
    for derived_cert, rec in organism_records:
        final_cert = derived_cert
        if derived_cert not in bootstrap_by_cert:
            candidates = unclaimed_bootstrap_by_name.get(normalize_name(rec["commercial_name"]), [])
            if len(candidates) == 1:
                final_cert = candidates[0]
        if final_cert:
            by_cert.setdefault(final_cert, []).append(rec)
    return by_cert


def build_canonical(cert_number: str, bootstrap_rec: dict | None,
                     organism_recs: list, today: date) -> dict:
    # Prefer the organism-page record when several duplicate pages listed the
    # same certificate (e.g. a Listeria method appearing on both the
    # "Listeria spp." and "Listeria spp. et L. monocytogenes" pages) --
    # arbitrarily the first is fine, they describe the same certificate.
    org_rec = organism_recs[0] if organism_recs else None

    commercial_name = (
        (org_rec["commercial_name"] if org_rec else None)
        or (bootstrap_rec["commercial_name"] if bootstrap_rec else None)
        or "UNKNOWN"
    )

    manufacturer = {"name": None, "address_raw": None,
                     "represented_in_europe": None, "site_of_production": None}
    if org_rec and org_rec["manufacturer"]["name"]:
        manufacturer["name"] = org_rec["manufacturer"]["name"]
    if bootstrap_rec and bootstrap_rec.get("company_holder_raw"):
        manufacturer["address_raw"] = bootstrap_rec["company_holder_raw"]
        if not manufacturer["name"]:
            m = re.search(r'certification\s*:?\s*([A-Z][A-Za-z0-9&.,\'\- ]{2,60})', bootstrap_rec["company_holder_raw"])
            if m:
                manufacturer["name"] = m.group(1).strip()

    method_type = {"action": None, "category": None}
    if org_rec:
        method_type = dict(org_rec["method_type"])
    elif bootstrap_rec:
        method_type["action"] = bootstrap_rec.get("action")

    target_organism = {"normalized": None, "raw": None}
    if org_rec:
        target_organism = dict(org_rec["target_organism"])
    elif bootstrap_rec and bootstrap_rec.get("target_organism"):
        target_organism = {"normalized": bootstrap_rec["target_organism"], "raw": bootstrap_rec.get("aim_of_method_raw")}

    reference_method = {"standard": None, "raw": None}
    if org_rec:
        reference_method = dict(org_rec["reference_method"])

    validation_scope = {"raw": "", "matrices": [], "excluded_matrices": [], "max_test_portion_g": None}
    if bootstrap_rec and bootstrap_rec.get("validation_scope_raw"):
        validation_scope["raw"] = bootstrap_rec["validation_scope_raw"]
    elif org_rec:
        validation_scope["raw"] = "(not published on the organism listing page; see certificate PDF)"

    certification = {
        "original_date": None, "renewal_dates": [], "extension_dates": [],
        "current_expiry": None, "status": "unknown",
    }
    if bootstrap_rec:
        certification["original_date"] = bootstrap_rec.get("certification_date")
        certification["current_expiry"] = bootstrap_rec.get("end_of_validity")
        renewals, extensions = split_renewal_extension_dates(bootstrap_rec.get("raw_block", ""))
        certification["renewal_dates"] = renewals
        certification["extension_dates"] = extensions
        certification["status"] = compute_status(certification["current_expiry"], today)

    traceability = {
        "source_document_type": "live_web_page" if org_rec else "certificate_list_pdf",
        "source_document_url": org_rec["traceability"]["source_document_url"] if org_rec else None,
        "extraction_date": None,
        "extraction_confidence": "high" if (org_rec and bootstrap_rec) else "medium",
        "notes": None,
    }
    extra_doc_links = {}
    if org_rec:
        extra_doc_links["certificate_pdf_url"] = org_rec["traceability"].get("certificate_pdf_url")
        extra_doc_links["summary_report_pdf_url"] = org_rec["traceability"].get("summary_report_pdf_url")
        extra_doc_links["organism_page_last_updated_raw"] = org_rec.get("organism_page_last_updated_raw")
    if not org_rec:
        traceability["notes"] = "No matching live organism-page record found; fields limited to what the bootstrap PDF-list import captured."
    if not bootstrap_rec:
        traceability["notes"] = "No matching bootstrap PDF-list record found (likely a certificate added/renamed after the bootstrap PDF was issued); no renewal history or validation-scope text available yet."

    return {
        "id": f"nf-validation--{re.sub(r'[^a-z0-9]+', '-', cert_number.lower()).strip('-')}",
        "source": "NF-VALIDATION",
        "source_certificate_number": cert_number,
        "commercial_name": commercial_name,
        "manufacturer": manufacturer,
        "method_type": method_type,
        "target_organism": target_organism,
        "reference_method": reference_method,
        "validation_scope": validation_scope,
        "study_design": {"protocol_standard": "EN ISO 16140-2:2016", "study_type": "unknown",
                          "number_of_labs": None, "number_of_samples": None},
        "certification": certification,
        "performance": None,
        "traceability": {**traceability, **extra_doc_links},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap-dir", default="data/nf_validation")
    ap.add_argument("--organism-pages-dir", default="data/nf_validation_organism_pages")
    ap.add_argument("--schema", default="schema/method.schema.json")
    ap.add_argument("--out-dir", default="data/methods")
    ap.add_argument("--today", default=None, help="Override today's date (YYYY-MM-DD) for status computation.")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()

    bootstrap_by_cert = load_bootstrap(Path(args.bootstrap_dir))
    organism_records = load_organism_pages(Path(args.organism_pages_dir))
    organism_by_cert = reconcile_organism_pages(organism_records, bootstrap_by_cert)

    all_certs = sorted(set(bootstrap_by_cert) | set(organism_by_cert))
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    both = only_bootstrap = only_organism = invalid = 0
    for cert in all_certs:
        boot = bootstrap_by_cert.get(cert)
        org = organism_by_cert.get(cert)
        if boot and org:
            both += 1
        elif boot:
            only_bootstrap += 1
        elif org:
            only_organism += 1

        rec = build_canonical(cert, boot, org or [], today)
        errors = list(validator.iter_errors(rec))
        if errors:
            invalid += 1
            print(f"SCHEMA ERROR [{cert}]: {errors[0].message}", file=sys.stderr)
            continue

        fname = re.sub(r'[^a-z0-9]+', '_', cert.lower()).strip('_') + ".json"
        (out_dir / fname).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Merged {len(all_certs)} certificates -> {out_dir} "
        f"(both sources: {both}, bootstrap-only: {only_bootstrap}, "
        f"organism-page-only: {only_organism}, schema-invalid: {invalid})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
