"""Mine performance data from an NF-Validation summary validation report PDF
(the document linked as `traceability.summary_report_pdf_url` on 135 of the
142 data/methods/ NF-Validation records).

These reports all follow the same fixed cover-page template ("NF VALIDATION
... Summary report ... Validation study according to the EN ISO 16140-2:2016
... (Certificate number: X) ... Quantitative/Qualitative method"), which lets
the certificate number and method nature be read directly off the PDF rather
than supplied by hand.

Scope of this first pass -- built and tested against exactly ONE real report
(TEMPO EB / BIO 12/21-12/06, a quantitative/enumeration method):
  - relative_trueness_by_category: reliably extracted from the clean
    per-category summary table ("Category n T(...)= SD ... Bias Lower limit
    (95%) Upper limit (95%)"), which is a standard ISO 16140-2 calculation
    output and plausibly recurs in this shape across other reports.
  - accuracy_profile.acceptability_limit_log: extracted from the stated
    "Acceptability Limit fixed at +/- X log" sentence.
  - accuracy_profile.by_matrix (SD repeatability per matrix): matrices are
    named by ISO 16140-2 food CATEGORY rather than the specific product
    tested, after rendering a page to an image with pymupdf and discovering
    the more specific product name (from chart captions) can come out
    reordered relative to the SD-repeatability table sequence even on the
    same page -- category order was checked the same way and does hold, so
    that's the anchor used. samples_out_of_beta_eti is left empty for every
    matrix rather than guess which category a named outlier product belongs
    to.
  - inclusivity/exclusivity: extracted from this report's specific narrative
    wording ("Same results were observed by both methods for N strains...");
    other reports may phrase this differently and will need the pattern
    broadened.
  - NOT mined: loq_log -- its source table's cells extracted as literal
    zeros for every matrix, a clear sign the table's structure defeats
    plain-text extraction; recording that would be worse than leaving it null.

Usage (offline, from a saved summary-report PDF):
    python3 summary_report_parser.py --pdf path/to/report.pdf --methods-dir data/methods
"""
import argparse
import json
import re
import sys
from pathlib import Path

import jsonschema
import pypdf


def to_float(s: str):
    if s is None:
        return None
    s = s.strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None



# Same certificate-number shape nf_validation_list_parser.py's CERT_START_RE
# validated against 138 real certificates (prefix + NN/NN-NN/NN + optional
# letter suffix) -- reused here rather than re-deriving it, since the format
# itself is already proven; only the surrounding label text varies.
CERT_NUMBER_FORMAT_RE = r'[A-Z0-9]{2,4}\s*\d{2}\s*/\s*\d{2}\s*-\s*\d{2}\s*/\s*\d{2}(?:\s*[A-Z])?'


def extract_cover_metadata(full_text: str) -> dict:
    # First run (against 140 real reports, only 1 developed against
    # offline): the exact "Certificate number:" English label alone missed
    # ~76/140. Widened to accept French phrasing too and, failing any
    # labeled match, fall back to the number format alone appearing
    # anywhere on the cover page -- still unverified against those specific
    # 76 failures (this environment can't fetch them to check), so treat
    # this as a improved-but-unproven second attempt, not a confirmed fix.
    cert_m = re.search(
        r'(?:Certificate\s+n(?:umber|o|°)|Num[ée]ro\s+de\s+certificat|N[o°]\s+de\s+certificat)\s*:?\s*\(?\s*'
        r'(' + CERT_NUMBER_FORMAT_RE + r')',
        full_text, re.I,
    )
    if not cert_m:
        cert_m = re.search(f'({CERT_NUMBER_FORMAT_RE})', full_text[:3000])
    certificate_number = re.sub(r'\s+', ' ', cert_m.group(1)).strip() if cert_m else None
    certificate_number = re.sub(r'\s*/\s*', '/', certificate_number) if certificate_number else None
    certificate_number = re.sub(r'\s*-\s*', '-', certificate_number) if certificate_number else None

    nature = None
    if re.search(r'\bQuantitative method\b', full_text):
        nature = "quantitative"
    elif re.search(r'\bQualitative method\b', full_text):
        nature = "qualitative"

    return {"certificate_number": certificate_number, "method_nature": nature}


def extract_category_names(full_text: str) -> dict:
    """'(Food) Category 1 Meat and meat products        ' -> {'1': 'Meat and meat products'}"""
    return dict(re.findall(r'\(Food\) Category (\d+)\s+([A-Za-z][A-Za-z ,]*?)\s{2,}', full_text))


def extract_relative_trueness_by_category(full_text: str, category_names: dict) -> list:
    """Parses the standard summary line:
    '<Category> <n> <T-stat> <SD> <half-width> <Bias> <Lower95> <Upper95>'
    e.g. '1 22 2,08 0,45 0,95 -0,02 -0,97 0,94'
    which appears right after a 'Category n T(...)= SD ... Bias Lower limit
    (95%) Upper limit (95%)' header row.
    """
    header_m = re.search(r'Category\s+n\s+T\([\d,]+;[\d,]+\)=?\s*SD.*?Upper limit \(95%\)', full_text, re.S)
    if not header_m:
        return []
    tail = full_text[header_m.end(): header_m.end() + 2000]
    row_re = re.compile(
        r'^\s*(\d+|All categories)\s+(\d+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+(-?[\d,\.]+)\s+(-?[\d,\.]+)\s+(-?[\d,\.]+)\s*$',
        re.M,
    )
    rows = []
    for cat_id, n, _t_stat, sd, _half_width, bias, lower, upper in row_re.findall(tail):
        if cat_id.lower() == "all categories":
            continue  # the aggregate row duplicates the per-category rows; keep only the per-category breakdown
        category_label = category_names.get(cat_id, f"Category {cat_id}")
        rows.append({
            "category": category_label,
            "bias_log": to_float(bias),
            "sd_log": to_float(sd),
            "n_samples": int(n),
            "n_interpretable": int(n),
            "lower_limit_95": to_float(lower),
            "upper_limit_95": to_float(upper),
        })
    return rows


def extract_acceptability_limit(full_text: str):
    m = re.search(r'Acceptability Limit[s]? (?:is |are )?fixed at\s*[±\+/\-]*\s*([\d,\.]+)\s*[Ll]og', full_text)
    return to_float(m.group(1)) if m else None


def extract_accuracy_profile_by_matrix(full_text: str, category_names: dict):
    """Pairs each category (in its established 1..N order -- the same
    category_names used for relative_trueness_by_category, read off Table 4)
    with its 'SD Repeatability <ref> <alt> +/- <AL>' line, matched by
    sequence position: one such line is emitted per category, and this
    report's category-summary tables (Table 4, the per-category relative
    trueness rows) all preserve category order in their text stream.

    This function deliberately does NOT use the accuracy-profile chart
    captions ("...Reference Median / <matrix name> / Bias / β-ETI...") to
    name matrices at the specific product level (e.g. "Ground beef" instead
    of "Meat and meat products") -- checking TEMPO EB's actual rendered page
    against its extracted text (see scratch notes in this repo's dev
    history) showed the chart-caption sequence can be reordered relative to
    the table sequence even on the very same page, presumably because the
    two are separate drawn objects in the PDF's content stream. Category
    names are the one signal confirmed to stay in order, so that's what's
    used, at the cost of losing some naming granularity.

    samples_out_of_beta_eti is deliberately left empty: attributing an
    individual outlier sample (from the "outside the Acceptability Limit"
    narrative, which names specific products, not categories) back to the
    correct category would need the same product<->category mapping this
    function just established isn't safely extractable -- recording a
    guessed attribution would risk mislabeling which matrix a real
    discrepancy belongs to, which is worse than omitting it.
    """
    ordered_categories = [category_names[k] for k in sorted(category_names, key=int)]
    sd_matches = re.findall(
        r'SD Repeatability\s+([\d,\.]+)\s+([\d,\.]+)\s+\+/-\s*([\d,\.]+)', full_text,
    )
    if not ordered_categories or len(ordered_categories) != len(sd_matches):
        return []  # counts disagree -- don't guess at a pairing we can't verify

    return [
        {
            "matrix": category,
            "sd_repeatability_reference": to_float(ref_sd),
            "sd_repeatability_alternative": to_float(alt_sd),
            "samples_out_of_beta_eti": [],
        }
        for category, (ref_sd, alt_sd, _al) in zip(ordered_categories, sd_matches)
    ]


def extract_inclusivity_exclusivity(full_text: str):
    """First-pass, single-report-tested narrative parsing (see module
    docstring). Returns (inclusivity, exclusivity) strain_panel_result dicts,
    each {} if this report's wording doesn't match.

    Operates on whitespace-collapsed text throughout: PDF text extraction
    inserts line-wrap newlines mid-sentence (e.g. "differences \\nwere
    observed"), so matching against raw newlines is unreliable -- only the
    collapsed form is matched against.
    """
    norm = re.sub(r'\s+', ' ', full_text)
    incl = {}
    excl = {}

    tested_m = re.findall(
        r'(\d+) (?:additional )?target strains and (\d+)(?:\s+additional)? non-target strains were tested',
        norm,
    )
    n_target_tested = sum(int(a) for a, _ in tested_m) if tested_m else None

    incl_m = re.search(
        r'Same results were observed by both methods for (\d+) strains\.\s*'
        r'For (\d+) strains?,\s*differences\s+were\s+observed:?\s*(.*?)(?:\s+Exclusivity\b|$)',
        norm,
    )
    if incl_m and n_target_tested:
        discrepancy_text = incl_m.group(3)
        discrepancy_lines = [l.strip(" -.") for l in re.split(r'\s-\s', discrepancy_text) if l.strip(" -.")]
        discrepancies = []
        for line in discrepancy_lines:
            # "<strain name/ref> was/is <rest of sentence>" -> split strain from note.
            split_m = re.match(r'(.+?)\s+(was|is|were)\s+(.*)', line)
            if split_m:
                discrepancies.append({"strain": split_m.group(1), "note": f"{split_m.group(2)} {split_m.group(3)}"})
            else:
                discrepancies.append({"strain": line, "note": None})
        incl = {
            "n_tested": n_target_tested,
            "n_correctly_detected": int(incl_m.group(1)),
            "discrepancies": discrepancies,
        }

    excl_m = re.search(
        # pypdf occasionally inserts a stray space inside a word or around a
        # hyphen ("non -target", "enum erated") -- tolerate both.
        r'Among the (\d+) non\s*-\s*target strains? tested,\s*(\d+) strains? were not enum\s*\w*ed with the .+?\s+method',
        norm,
    )
    if excl_m:
        excl_named_m = re.search(r'strains were enumerated by both methods:\s*(.*?)\.', norm)
        named = [s.strip() for s in re.split(r',(?! [A-Z][a-z]+\s\d)|\band\b', excl_named_m.group(1))] if excl_named_m else []
        excl = {
            "n_tested": int(excl_m.group(1)),
            "n_correctly_detected": int(excl_m.group(2)),
            "discrepancies": [{"strain": s, "note": "Cross-reactive: enumerated by the alternative method"} for s in named if s],
        }

    return incl, excl


def mine_performance(pdf_path: Path) -> dict:
    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(p.extract_text() or "" for p in reader.pages)

    cover = extract_cover_metadata(full_text)
    category_names = extract_category_names(full_text)
    relative_trueness = extract_relative_trueness_by_category(full_text, category_names)
    acceptability_limit = extract_acceptability_limit(full_text)
    by_matrix = extract_accuracy_profile_by_matrix(full_text, category_names)
    inclusivity, exclusivity = extract_inclusivity_exclusivity(full_text)

    performance = None
    if cover["method_nature"] == "quantitative":
        performance = {
            "method_nature": "quantitative",
            "quantitative": {
                "relative_trueness_by_category": relative_trueness,
                "accuracy_profile": {
                    "acceptability_limit_log": acceptability_limit,
                    "by_matrix": by_matrix,
                },
                "loq_log": None,
                "inclusivity": inclusivity,
                "exclusivity": exclusivity,
            },
        }
    elif cover["method_nature"] == "qualitative":
        performance = {
            "method_nature": "qualitative",
            "qualitative": {
                "method_comparison_by_category": [],
                "inclusivity": inclusivity,
                "exclusivity": exclusivity,
            },
        }

    return {
        "certificate_number": cover["certificate_number"],
        "method_nature": cover["method_nature"],
        "performance": performance,
        "mining_notes": (
            "Mined from the summary validation report PDF. relative_trueness_by_category "
            "(quantitative) is reliably extracted from a standard ISO 16140-2 summary table. "
            "accuracy_profile.by_matrix names each matrix by its ISO 16140-2 food CATEGORY "
            "(e.g. 'Dairy products'), not the specific product tested (e.g. 'Milk') -- an "
            "earlier version tried the more specific product name from the accuracy-profile "
            "chart captions, but checking a rendered page image against the extracted text "
            "showed that caption sequence can be reordered relative to the SD-repeatability "
            "table sequence even on the same page (a chart is a separate drawn object from "
            "its table), which silently mispaired two matrices. Category order was checked "
            "the same way and does hold, so that's the safe anchor used instead; the extractor "
            "refuses to pair values at all (returns []) if the category count and "
            "SD-repeatability-line count disagree. samples_out_of_beta_eti is left empty for "
            "every matrix: the outside-the-limit narrative names specific products, and "
            "mapping those back to the right category isn't safely extractable, so leaving it "
            "empty beats a guessed attribution -- see the report itself for that detail. "
            "loq_log is NOT mined -- its source table's cell values did not extract as real "
            "numbers at all (came back as literal zeros), a clear sign the table structure "
            "defeats plain-text extraction; left null rather than record that placeholder. "
            "Inclusivity/exclusivity narrative parsing has only been tested against one report "
            "(TEMPO EB) and its exact wording -- other reports may need pattern adjustments."
        ),
    }


def merge_into_method_record(mined: dict, methods_dir: Path, schema_path: Path) -> bool:
    cert = mined["certificate_number"]
    if not cert:
        print("No certificate number found on the report's cover page; aborting merge.", file=sys.stderr)
        return False

    fname = re.sub(r'[^a-z0-9]+', '_', cert.lower()).strip('_') + ".json"
    record_path = methods_dir / fname
    if not record_path.exists():
        print(f"No existing data/methods/ record for certificate {cert} ({fname}); aborting merge.", file=sys.stderr)
        return False

    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["performance"] = mined["performance"]
    existing_notes = record["traceability"].get("notes") or ""
    if mined["mining_notes"] not in existing_notes:
        record["traceability"]["notes"] = (existing_notes + " " + mined["mining_notes"]).strip()

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(record))
    if errors:
        print(f"SCHEMA ERROR merging performance data into {fname}: {errors[0].message}", file=sys.stderr)
        return False

    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Merged performance data into {record_path}", file=sys.stderr)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--methods-dir", default="data/methods")
    ap.add_argument("--schema", default="schema/method.schema.json")
    args = ap.parse_args()

    mined = mine_performance(Path(args.pdf))
    print(json.dumps(mined, ensure_ascii=False, indent=2))
    merge_into_method_record(mined, Path(args.methods_dir), Path(args.schema))


if __name__ == "__main__":
    main()
