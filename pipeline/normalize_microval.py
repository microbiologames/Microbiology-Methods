"""Transform raw MicroVal certificate rows (data/microval/) into canonical
method records under data/methods/, validated against schema/method.schema.json.

MicroVal's live-fetched table (scrapers/microval_live_fetch.py) gives exactly
6 columns per certificate: Analyte, Certificate number, Test kit name,
Supplier - manufacturer, Expiry date, Status. There is no explicit
technology/principle field on MicroVal's public listing -- method_type.category
is inferred from the commercial name via infer_method_category() below, which
only fires on well-known, unambiguous product-family/brand keywords (Petrifilm,
Compact Dry, Easy Plate, chromogenic agar lines, PCR kit naming, etc.); a name
that matches nothing stays "other" rather than being guessed, since a wrong
guess would be worse than an honest unknown. method_type.action (detection
vs. enumeration vs. confirmation) is left null -- the commercial name alone
doesn't reliably say which, and guessing that would misrepresent the method's
actual use. Confirmed real, not speculative: field names and values were
read from actual captured pages (see scrapers/microval_live_fetch.py).
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import jsonschema

# MicroVal's analyte wording already matches the vocabulary used elsewhere
# in this project almost everywhere; only the handful of variants actually
# observed in the two real captured pages need mapping (not a general rule
# -- e.g. blindly splitting on ";" would wrongly drop "Listeria monocytogenes"
# from a genuinely compound target instead of recognizing it matches an
# existing combined label already used by NF-Validation records).
ANALYTE_NORMALIZE = {
    "bacillus cereus group": "Bacillus cereus",
    "listeria spp.; listeria monocytogenes": "Listeria spp. and L. monocytogenes",
    "salmonella spp.; typing of 106 serovars": "Salmonella spp.",
}


def normalize_analyte(raw: str) -> str:
    if not raw:
        return raw
    key = raw.strip().lower().rstrip(".")
    return ANALYTE_NORMALIZE.get(key, raw.strip())


# Keyword -> method_type.category, checked in order against the lowercased
# commercial name (first match wins). Every entry here is a real, checkable
# product-family or technology keyword, not a guess at what a name "sounds
# like" -- each is documented with why it belongs in that bucket, so a
# reviewer can verify or correct any single line without re-deriving the
# whole table. New MicroVal certificates just need their name to contain
# one of these; a name matching none of them stays "other" rather than
# being forced into the nearest-sounding bucket (see infer_method_category).
#
# NOTE: matches on brand/format keywords, so a legitimate exception (e.g. a
# product genuinely named "Compact Dry" that isn't Shimadzu's dehydrated
# media plate line) would misclassify -- not observed in this project's
# real data, but worth knowing if that ever changes.
_CATEGORY_KEYWORDS = [
    # Ready-to-use dehydrated culture media in plate/film format (poured
    # media substitutes -- the exact family the project owner flagged as
    # wrongly landing in "other"): 3M Petrifilm, Shimadzu Compact Dry,
    # Kikkoman Easy Plate, JNC MC-Media Pad, Charm PeelPlate, Neogen One
    # Plate all use this dry-rehydratable-film format.
    ("petrifilm", "culture_media"),
    ("compact dry", "culture_media"),
    ("easy plate", "culture_media"),
    ("mc-media pad", "culture_media"),
    ("peelplate", "culture_media"),
    ("peel plate", "culture_media"),
    ("one plate", "culture_media"),
    ("count plate", "culture_media"),
    # Poured/prepared chromogenic agar (a schema category distinct from the
    # ready-plate formats above): Thermo Fisher's Brilliance line, Oxoid/
    # bioMerieux's CampyFood agar and CASA (Campylobacter Selective Agar)
    # are all classic chromogenic plated media, not a dry-film format.
    ("brilliance", "chromogenic_agar"),
    ("campyfood", "chromogenic_agar"),
    ("casa", "chromogenic_agar"),
    ("chromogenic", "chromogenic_agar"),
    # Nucleic-acid amplification: explicit PCR/qPCR naming, and Merck's
    # "GDS" (Genetic Detection System) and Bio-Rad's "iQ-Check" product
    # lines, which are both real-time PCR kits.
    ("pcr", "molecular_pcr"),
    (" gds ", "molecular_pcr"),
    ("iq-check", "molecular_pcr"),
    ("genetic detection", "molecular_pcr"),
    # Antibody-based detection.
    ("elisa", "immunological_elisa"),
    ("immunoassay", "immunological_elisa"),
    ("lateral flow", "immunological_elisa"),
    # Flow cytometry.
    ("cytomet", "flow_cytometry"),
    # MALDI-TOF mass-spectrometry identification (Bruker MALDI Biotyper,
    # Autobio's Autof ms): identifies by protein/biochemical fingerprint
    # rather than nucleic acid, antibody, or culture growth, so
    # "biochemical" is the closest existing schema bucket -- not a perfect
    # fit, but truer than "other" for a real, well-documented technology.
    ("maldi", "biochemical"),
    ("biotyper", "biochemical"),
    ("autof ms", "biochemical"),  # Autobio's own MALDI-TOF instrument line
    ("mass spectrometry", "biochemical"),
    # Neogen's Soleris: an automated optical/colorimetric system reading a
    # pH- or CO2-sensitive indicator in a sealed vial as the organism
    # grows -- a biochemical detection principle, not a plated culture
    # medium a lab pours or reads directly.
    ("soleris", "biochemical"),
    # bioMerieux's TEMPO: automated miniaturized-MPN enumeration in a
    # sealed multi-well card read by fluorogenic/chromogenic substrate
    # hydrolysis -- a biochemical detection principle, not a plated medium.
    ("tempo", "biochemical"),
]


def infer_method_category(commercial_name: str):
    """Best-effort method_type.category from a MicroVal commercial name --
    see _CATEGORY_KEYWORDS for the exact rules and their justification.
    Returns None (not "other") on no match, so callers can tell "genuinely
    unclassifiable" apart from "classified as other" if they ever need to."""
    if not commercial_name:
        return None
    name = commercial_name.lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in name:
            return category
    return None


def parse_ddmmyyyy_date(raw: str):
    """Both expiry_date_raw and first_approval_date_raw (from the detail
    page) share this real DD-MM-YYYY format, confirmed on actual captured
    pages -- see scrapers/microval_live_fetch.py."""
    m = re.match(r'(\d{2})-(\d{2})-(\d{4})', raw or "")
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def compute_status(status_raw: str, current_expiry: str, today: date) -> str:
    status_raw = (status_raw or "").strip().lower()
    if status_raw == "valid":
        if current_expiry:
            try:
                y, m, d = (int(x) for x in current_expiry.split("-"))
                return "active" if date(y, m, d) >= today else "expired"
            except ValueError:
                pass
        return "active"
    if status_raw in ("not renewed", "withdrawn", "expired"):
        return "expired"
    return "unknown"


def build_canonical(raw: dict, today: date) -> dict:
    cert_number = raw["certificate_number"]
    current_expiry = parse_ddmmyyyy_date(raw.get("expiry_date_raw"))
    original_date = parse_ddmmyyyy_date(raw.get("first_approval_date_raw"))

    matrices_raw = raw.get("matrices_raw")
    matrices = [m.strip() for m in matrices_raw.split(",")] if matrices_raw else []

    summary_report_pdf_url = raw.get("summary_report_pdf_url")
    traceability_notes = (
        f"MicroVal listing page label: {raw.get('label')!r}. Raw status text: "
        f"{raw.get('status_raw')!r}, raw expiry text: {raw.get('expiry_date_raw')!r}."
    )
    if raw.get("certificate_pdf_url") is not None:
        # Only certificates whose detail page was actually fetched (see
        # microval_live_fetch.py's second pass) carry this field, so its
        # presence is what tells "study report confirmed not published" apart
        # from "detail page was never fetched for this record".
        if summary_report_pdf_url is None:
            traceability_notes += " Study report not published on MicroVal's certificate detail page."
        else:
            traceability_notes += " Study report available for further mining."

    return {
        "id": f"microval--{re.sub(r'[^a-z0-9]+', '-', cert_number.lower()).strip('-')}",
        "source": "MICROVAL",
        "source_certificate_number": cert_number,
        "commercial_name": raw["commercial_name"],
        "manufacturer": {
            "name": raw.get("manufacturer_raw"),
            "address_raw": None,
            "represented_in_europe": None,
            "site_of_production": None,
        },
        "method_type": {"action": None, "category": infer_method_category(raw.get("commercial_name"))},
        "target_organism": {
            "normalized": normalize_analyte(raw.get("analyte_raw")),
            "raw": raw.get("analyte_raw"),
        },
        "reference_method": {
            "standard": raw.get("reference_method_raw"),
            "raw": raw.get("reference_method_raw"),
        },
        "validation_scope": {
            "raw": matrices_raw or "(not published on MicroVal's public certificate listing)",
            "matrices": matrices,
            "excluded_matrices": [],
            "max_test_portion_g": None,
        },
        "study_design": {
            "protocol_standard": "EN ISO 16140-2:2016",
            "study_type": "unknown",
            "number_of_labs": None,
            "number_of_samples": None,
        },
        "certification": {
            "original_date": original_date,
            "renewal_dates": [],
            "extension_dates": [],
            "current_expiry": current_expiry,
            "status": compute_status(raw.get("status_raw"), current_expiry, today),
        },
        "performance": None,
        "traceability": {
            "source_document_type": "live_web_page",
            "source_document_url": raw.get("source_page_url"),
            "summary_report_pdf_url": summary_report_pdf_url,
            "extraction_date": None,
            "extraction_confidence": "high",
            "notes": traceability_notes,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="data/microval")
    ap.add_argument("--schema", default="schema/method.schema.json")
    ap.add_argument("--out-dir", default="data/methods")
    ap.add_argument("--today", default=None)
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = skipped = invalid = unclassified = 0
    for f in sorted(Path(args.raw_dir).glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        if "certificate_number" not in raw or "commercial_name" not in raw:
            # A row whose column count didn't match the expected header
            # (see extract_table_rows) -- has no reliable fields to build from.
            skipped += 1
            continue

        rec = build_canonical(raw, today)
        errors = list(validator.iter_errors(rec))
        if errors:
            invalid += 1
            print(f"SCHEMA ERROR [{raw['certificate_number']}]: {errors[0].message}", file=sys.stderr)
            continue

        if rec["method_type"]["category"] is None:
            unclassified += 1
            print(f"UNCLASSIFIED method_type.category [{raw['commercial_name']!r}] -- "
                  f"no keyword in _CATEGORY_KEYWORDS matched; add one if this is a known "
                  f"product family.", file=sys.stderr)

        fname = f"microval_{re.sub(r'[^A-Za-z0-9]+', '_', raw['certificate_number'])}.json"
        (out_dir / fname).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    print(f"Normalized {written} MicroVal certificates -> {out_dir} "
          f"(skipped unparsed rows: {skipped}, schema-invalid: {invalid}, "
          f"method_type.category unclassified: {unclassified})", file=sys.stderr)


if __name__ == "__main__":
    main()
