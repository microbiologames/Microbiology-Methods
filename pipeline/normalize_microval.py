"""Transform raw MicroVal certificate rows (data/microval/) into canonical
method records under data/methods/, validated against schema/method.schema.json.

MicroVal's live-fetched table (scrapers/microval_live_fetch.py) gives exactly
6 columns per certificate: Analyte, Certificate number, Test kit name,
Supplier - manufacturer, Expiry date, Status. There is no equivalent of
NF-Validation's validation-scope text or method category here -- MicroVal's
public listing simply doesn't expose that, so those fields stay null rather
than guessed. Confirmed real, not speculative: field names and values were
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


def parse_expiry_date(raw: str):
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
    current_expiry = parse_expiry_date(raw.get("expiry_date_raw"))

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
        "method_type": {"action": None, "category": None},
        "target_organism": {
            "normalized": normalize_analyte(raw.get("analyte_raw")),
            "raw": raw.get("analyte_raw"),
        },
        "reference_method": {"standard": None, "raw": None},
        "validation_scope": {
            "raw": "(not published on MicroVal's public certificate listing)",
            "matrices": [],
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
            "original_date": None,
            "renewal_dates": [],
            "extension_dates": [],
            "current_expiry": current_expiry,
            "status": compute_status(raw.get("status_raw"), current_expiry, today),
        },
        "performance": None,
        "traceability": {
            "source_document_type": "live_web_page",
            "source_document_url": raw.get("source_page_url"),
            "extraction_date": None,
            "extraction_confidence": "high",
            "notes": (
                f"MicroVal listing page label: {raw.get('label')!r}. Raw status text: "
                f"{raw.get('status_raw')!r}, raw expiry text: {raw.get('expiry_date_raw')!r}."
            ),
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

    written = skipped = invalid = 0
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

        fname = f"microval_{re.sub(r'[^A-Za-z0-9]+', '_', raw['certificate_number'])}.json"
        (out_dir / fname).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    print(f"Normalized {written} MicroVal certificates -> {out_dir} "
          f"(skipped unparsed rows: {skipped}, schema-invalid: {invalid})", file=sys.stderr)


if __name__ == "__main__":
    main()
