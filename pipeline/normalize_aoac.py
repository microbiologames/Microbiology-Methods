"""Transform raw AOAC-RI certificate records (data/aoac_ptm/) into canonical
method records under data/methods/, validated against schema/method.schema.json.

Unlike NF-Validation, AOAC-RI has only one collector today (the certificate
PDF bundles both certification metadata and validation-study performance
data), so there is nothing to reconcile across sources yet -- this is a
straight shape transform, not a merge.
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import jsonschema


def compute_status(current_expiry: str | None, today: date) -> str:
    if not current_expiry:
        return "unknown"
    try:
        y, m, d = (int(x) for x in current_expiry.split("-"))
        return "active" if date(y, m, d) >= today else "expired"
    except ValueError:
        return "unknown"


def build_canonical(raw: dict, today: date) -> dict:
    cert_number = raw["certificate_number"]

    has_independent_lab = bool(raw.get("independent_laboratory_raw"))

    incl = raw.get("inclusivity") or {}
    excl = raw.get("exclusivity") or {}
    has_selectivity_data = incl.get("n_tested") is not None or excl.get("n_tested") is not None

    performance = None
    if has_selectivity_data:
        performance = {
            "method_nature": "qualitative",
            "qualitative": {
                "method_comparison_by_category": [],
                "inclusivity": incl if incl.get("n_tested") is not None else {},
                "exclusivity": excl if excl.get("n_tested") is not None else {},
            },
        }

    notes = (
        "AOAC-RI certificate PDFs bundle certification metadata AND validation-study "
        "performance data in one document (unlike NF-Validation's separate cert + "
        "summary-report). Inclusivity/exclusivity extraction is best-effort text mining "
        "(narrative counts where stated, else counted from per-strain table rows -- "
        "table rows wrapped across two PDF text lines can be undercounted). The "
        "presumptive-vs-confirmed / vs-reference-method POD comparison tables are not "
        "yet mined (table layout is not reliably recoverable from PDF text extraction). "
        "This parser has only been developed against 4 example certificates; broaden "
        "coverage once more are available."
    )

    return {
        "id": f"aoac-ri--{re.sub(r'[^a-z0-9]+', '-', cert_number.lower()).strip('-')}",
        "source": "AOAC-RI",
        "source_certificate_number": cert_number,
        "commercial_name": raw.get("method_name") or "UNKNOWN",
        "manufacturer": {
            "name": raw.get("manufacturer_name"),
            "address_raw": raw.get("manufacturer_address_raw"),
            "represented_in_europe": None,
            "site_of_production": None,
        },
        "method_type": {
            "action": "detection",
            "category": raw.get("category_guess"),
        },
        "target_organism": {
            "normalized": raw.get("target_organism"),
            "raw": raw.get("target_organism_raw"),
        },
        "reference_method": {
            "standard": None,
            "raw": raw.get("reference_method_raw"),
        },
        "validation_scope": {
            "raw": raw.get("matrices_raw") or "(not stated in a single free-text scope field on this certificate)",
            "matrices": raw.get("matrices") or [],
            "excluded_matrices": [],
            "max_test_portion_g": None,
        },
        "study_design": {
            "protocol_standard": "AOAC Performance Tested Methods (PTM) Program",
            "study_type": "multi-lab" if has_independent_lab else "unknown",
            "number_of_labs": 2 if has_independent_lab else None,
            "number_of_samples": None,
        },
        "certification": {
            "original_date": raw.get("original_certification_date"),
            "renewal_dates": [],
            "extension_dates": [],
            "current_expiry": raw.get("expiration_date"),
            "status": compute_status(raw.get("expiration_date"), today),
        },
        "performance": performance,
        "traceability": {
            "source_document_type": "certificate_pdf",
            "source_document_url": None,
            "extraction_date": None,
            "extraction_confidence": "medium",
            "notes": notes,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="data/aoac_ptm")
    ap.add_argument("--schema", default="schema/method.schema.json")
    ap.add_argument("--out-dir", default="data/methods")
    ap.add_argument("--today", default=None)
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = invalid = 0
    for f in sorted(Path(args.raw_dir).glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        rec = build_canonical(raw, today)
        errors = list(validator.iter_errors(rec))
        if errors:
            invalid += 1
            print(f"SCHEMA ERROR [{raw['certificate_number']}]: {errors[0].message}", file=sys.stderr)
            continue
        fname = f"aoac_{raw['certificate_number']}.json"
        (out_dir / fname).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    print(f"Normalized {written} AOAC-RI certificates -> {out_dir} (schema-invalid: {invalid})", file=sys.stderr)


if __name__ == "__main__":
    main()
