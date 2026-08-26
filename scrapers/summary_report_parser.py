"""Mine performance data from an NF-Validation summary validation report PDF
(the document linked as `traceability.summary_report_pdf_url` on 135 of the
142 data/methods/ NF-Validation records).

These reports all follow the same fixed cover-page template ("NF VALIDATION
... Summary report ... Validation study according to the EN ISO 16140-2:2016
... (Certificate number: X) ... Quantitative/Qualitative method"), which lets
the certificate number and method nature be read directly off the PDF rather
than supplied by hand.

Category-breakdown extraction (relative_trueness_by_category for quantitative
reports, method_comparison_by_category for qualitative ones) is built on
pdfplumber's structural table extraction (the real cell grid, from the PDF's
visual layout) rather than a text regex -- this replaced an earlier
regex-based version that was built and tested against exactly one real
report (TEMPO EB) and confirmed, once checked against the real merged data
from a full mining run, to fail on the large majority of other reports: each
expert laboratory phrases its table header differently for what is
conceptually the same table, and at least one renders its "D-bar (bias)"
symbol as garbled Unicode glyphs no text regex would match, while the actual
cell structure (which column holds "SD", which holds "95% lower limit")
stayed reliably extractable. See find_tables_by_header()'s docstring for the
detection approach, and _walk_category_rows() for the two real per-category
row shapes (flat, and hierarchical-with-a-Total-row) it's confirmed to
handle. method_comparison_by_category was previously not implemented at all
(hardcoded to []) since no real qualitative report had been inspected; now
extracted from a third row shape confirmed against a real qualitative
report, where a category's id/name and its first sub-item's data share one
row -- this doesn't line up column-for-column with the header the way the
quantitative shapes do, so its SE alt / SE ref values are read by position
counted back from the end of the row (see _offset_from_end) rather than by
zipping against the header from the front. rlod is left null in every case
-- it comes from a separate section/table (Relative Level of Detection)
neither function looks at.

Also mined, unchanged from the original version and still text-based (this
part held up fine, wasn't reason for the rewrite above):
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
  - inclusivity/exclusivity: extracted from one report's specific narrative
    wording ("Same results were observed by both methods for N strains...");
    other reports may phrase this differently and will need the pattern
    broadened -- not yet checked against the real merged data the way the
    category-breakdown extraction above was.
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
import pdfplumber
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


def _clean(text) -> str:
    return re.sub(r'\s+', ' ', str(text)).strip()


def _compact(row) -> list:
    """Drop the None/blank cells pdfplumber leaves for a merged cell's
    spanned columns, keeping only the cells that actually carry text."""
    return [_clean(c) for c in row if c and _clean(c)]


def _is_data_row(row) -> bool:
    """A row starts real per-category data once its first populated cell is
    a bare category id ('1', '2', ...) or an aggregate-row marker ('All
    categories' / 'All products') -- everything before that, across however
    many physical rows a wrapped header spans, is header."""
    first = next((c for c in row if c and _clean(c)), None)
    if first is None:
        return False
    first = _clean(first)
    return bool(re.match(r'^\d+$', first)) or bool(re.match(r'^all\s+(categor|product)', first, re.I))


def _merge_header_rows(rows) -> list:
    """Column-wise concatenation of however many physical rows a table's
    header wraps across (confirmed necessary against a real report: one
    table's header reads 'Category | n | (bias) | SD | 95% lower limit |
    95% upper limit' split across 4 separate physical rows, wrapped
    mid-label). Same-column text from later rows is appended, not
    overwritten, so a label like '95% lower' + 'limit' becomes one string."""
    width = max(len(r) for r in rows)
    merged = [None] * width
    for row in rows:
        for i in range(min(len(row), width)):
            cell = row[i]
            if cell and _clean(cell):
                text = _clean(cell)
                merged[i] = f"{merged[i]} {text}" if merged[i] else text
    return merged


def find_tables_by_header(pdf_path, required_keywords):
    """Yield (header_labels, data_rows) for every table anywhere in the PDF
    whose reconstructed header text contains every keyword in
    required_keywords (case-insensitive substring match against the whole
    joined header).

    Uses pdfplumber's structural table extraction (the real cell grid, from
    the PDF's visual layout) rather than matching header wording with a
    regex -- confirmed necessary, not a style preference: two real reports'
    conceptually-identical category-breakdown tables turned out to phrase
    their headers completely differently, and one renders its "D-bar
    (bias)" symbol as garbled Unicode glyphs that no reasonable text regex
    would match, while the surrounding cell structure (which column holds
    "SD", which holds "95% lower limit") stayed perfectly extractable.
    """
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                header_rows = []
                data_start = None
                for i, row in enumerate(table):
                    if _is_data_row(row):
                        data_start = i
                        break
                    header_rows.append(row)
                if data_start is None or not header_rows:
                    continue
                labels = _compact(_merge_header_rows(header_rows))
                haystack = " ".join(labels).lower()
                if not all(kw in haystack for kw in required_keywords):
                    continue
                yield labels, table[data_start:]


def _last_number(text):
    """Pull the trailing signed decimal number out of a cell that may carry
    leading junk -- confirmed necessary against a real report, where one
    category's bias value literally reads '𝑫𝑫 𝑫 -0,32' (a garbled Unicode
    rendering of the D-bar/bias symbol prefixed onto the real number)."""
    if not text:
        return None
    matches = re.findall(r'-?\d+[.,]?\d*', text)
    return matches[-1] if matches else None


def _find_metric(metrics: dict, *keyword_groups):
    """metrics: {header_label: value}. Each entry in keyword_groups is a
    tuple of keywords that must ALL appear (case-insensitive substring) in
    a label for it to match; groups are tried in order. Returns the first
    matching value."""
    for label, value in metrics.items():
        low = label.lower()
        for keywords in keyword_groups:
            if all(kw in low for kw in keywords):
                return value
    return None


def _walk_category_rows(header_labels, data_rows):
    """Yield (category_name, {header_label: value}) for each real
    per-category result row, handling the two table shapes confirmed
    against real reports:

    - flat: one row per category, e.g. ['1', 'Meat products', '47', bias,
      SD, lower, upper] -- category name inline with its data.
    - hierarchical: a category-label-only row (e.g. ['7', 'Powdered infant
      formula...']) followed by per-item sub-rows (a/b/c breakdowns, which
      this project isn't after) and then a 'Total' row carrying the
      category's real aggregate values -- category name and data are on
      different rows here.

    Distinguishing the two is done by comparing each row's compacted
    length to the header's: one extra compacted cell means an inline
    id+name pair (flat shape); one fewer means the category name was
    already consumed by an earlier label-only row (hierarchical shape).
    An aggregate "All categories"/"All products" row (which duplicates the
    per-category rows, not a category of its own) is always skipped.
    """
    current_category = None
    for row in data_rows:
        values = _compact(row)
        if not values:
            continue
        if re.match(r'^all\s+(categor|product)', values[0], re.I):
            continue
        if len(values) == len(header_labels) + 1:
            yield values[1], dict(zip(header_labels[1:], values[2:]))
        elif len(values) == len(header_labels) - 1 and values[0].lower() == "total":
            if current_category:
                yield current_category, dict(zip(header_labels[1:], values))
            current_category = None
        elif len(values) <= 2 and re.match(r'^\d+$', values[0]):
            current_category = values[-1]
        # else: an a/b/c sub-item row, or a shape this project hasn't seen
        # yet -- skipped rather than guessed at.


def extract_relative_trueness_by_category(pdf_path) -> list:
    for header_labels, data_rows in find_tables_by_header(pdf_path, ["category", "sd"]):
        rows = []
        for category, metrics in _walk_category_rows(header_labels, data_rows):
            sd = _find_metric(metrics, ("sd",))
            lower = _find_metric(metrics, ("lower",))
            upper = _find_metric(metrics, ("upper",))
            if sd is None or lower is None or upper is None:
                continue  # doesn't look like the table this is meant to find after all
            bias = _find_metric(metrics, ("bias",))
            n = _find_metric(metrics, ("n",))
            rows.append({
                "category": category,
                "bias_log": to_float(_last_number(bias)),
                "sd_log": to_float(_last_number(sd)),
                "n_samples": int(n) if n and re.match(r'^\d+$', n) else None,
                "n_interpretable": int(n) if n and re.match(r'^\d+$', n) else None,
                "lower_limit_95": to_float(_last_number(lower)),
                "upper_limit_95": to_float(_last_number(upper)),
            })
        if rows:
            # First matching table wins -- a report covering several related
            # certificates in one PDF (confirmed real: one report combines
            # three Petrifilm incubation-time variants) has one such table
            # per certificate; picking the first is a known simplification,
            # not a guarantee of picking the right one for every certificate
            # in that PDF.
            return rows
    return []


def _offset_from_end(header_labels, keywords):
    """Index, counted back from the end of header_labels, of the first label
    matching every keyword (case-insensitive substring). Anchored from the
    end rather than the front: confirmed necessary against a real report
    whose qualitative table packs the category id, name, sub-item letter
    and sub-item name into leading cells of a data row all in one go (a
    third row shape _walk_category_rows doesn't recognize), so the leading
    column count drifts row to row while the trailing metric columns
    (SE alt, SE ref, RT%, FPR, FNR -- standardised by ISO 16140-2 itself)
    stay one-to-one with the header.

    Drops bare '%' cells before ranking: confirmed against the same report
    that a units-only header row (wrapping "SE alt" etc.'s '%' onto its own
    physical line) lands, after column-wise merge, as a standalone '%'
    label at a raw column offset from its actual metric -- diluting the
    real metrics' rank from the end. It carries no identifying information
    a value row could match anyway, so it's dropped rather than merged."""
    real_labels = [l for l in header_labels if l.strip() != '%']
    for i, label in enumerate(real_labels):
        if all(kw in label.lower() for kw in keywords):
            return len(real_labels) - i - 1
    return None


def _value_at_offset_from_end(values, offset):
    if offset is None:
        return None
    idx = -(offset + 1)
    if -idx > len(values):
        return None
    return values[idx]


def extract_method_comparison_by_category(pdf_path) -> list:
    """Qualitative-method equivalent of extract_relative_trueness_by_category
    -- previously not implemented at all (hardcoded to []) since no real
    report had been inspected. Confirmed against a real report's Table 4
    ('Calculation of relative trueness (RT), sensitivity (SE)... for the
    alternative method').

    Doesn't reuse _walk_category_rows: that helper's flat/hierarchical
    shapes assume a category's data lines up column-for-column with the
    header, which breaks here -- a category's first sub-item (a/b/c...) is
    packed onto the same row as the category id and name, at a different
    compacted width than the header. Reads the category id/name off
    whichever row starts with a bare number, then prefers a later 'Total'
    row (the real per-category aggregate across sub-items) as the data
    source when one follows, falling back to the id/name row itself
    otherwise, and pulls SE alt/SE ref from either by column position
    counted from the end of the row (see _offset_from_end)."""
    for header_labels, data_rows in find_tables_by_header(pdf_path, ["category", "se"]):
        se_alt_offset = _offset_from_end(header_labels, ("se", "alt"))
        se_ref_offset = _offset_from_end(header_labels, ("se", "ref"))
        if se_alt_offset is None or se_ref_offset is None:
            continue

        rows = []
        current_category = None
        current_row = None

        def flush():
            if current_category is None or current_row is None:
                return
            se_alt = _value_at_offset_from_end(current_row, se_alt_offset)
            se_ref = _value_at_offset_from_end(current_row, se_ref_offset)
            if se_alt is None or se_ref is None:
                return
            rows.append({
                "category": current_category,
                "sensitivity_alternative_pct": to_float(_last_number(se_alt)),
                "sensitivity_reference_pct": to_float(_last_number(se_ref)),
                # RLOD comes from a separate section/table (Relative Level of
                # Detection) this function doesn't look at -- not guessed.
                "rlod": None,
            })

        for row in data_rows:
            values = _compact(row)
            if not values:
                continue
            first = values[0]
            if re.match(r'^all\s+(categor|product)', first, re.I):
                continue
            if re.match(r'^\d+$', first):
                flush()
                name = values[1] if len(values) > 1 else None
                # Confirmed against a real report: a digit-prefixed row can
                # itself read like "1 Total ..." (some other row's aggregate
                # bleeding into what looks like a category id here), which
                # would otherwise surface "Total" as a fake category name.
                current_category = None if name and name.strip().lower() == "total" else name
                current_row = values
            elif first.lower() == "total":
                current_row = values
            # else: an a/b/c sub-item row, or a continuation fragment --
            # skipped, the Total row (or the id/name row as fallback)
            # already carries what's needed.
        flush()
        if rows:
            return rows
    return []


def extract_acceptability_limit(full_text: str):
    m = re.search(r'Acceptability Limit[s]? (?:is |are )?fixed at\s*[±\+/\-]*\s*([\d,\.]+)\s*[Ll]og', full_text)
    return to_float(m.group(1)) if m else None


def extract_accuracy_profile_by_matrix(full_text: str, ordered_categories: list):
    """Pairs each category (in the order extract_relative_trueness_by_category
    already returned them in) with its 'SD Repeatability <ref> <alt> +/- <AL>'
    line, matched by sequence position: one such line is emitted per
    category, and this report's category-summary table preserves category
    order in the surrounding text stream.

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
    acceptability_limit = extract_acceptability_limit(full_text)
    inclusivity, exclusivity = extract_inclusivity_exclusivity(full_text)

    performance = None
    if cover["method_nature"] == "quantitative":
        relative_trueness = extract_relative_trueness_by_category(pdf_path)
        by_matrix = extract_accuracy_profile_by_matrix(
            full_text, [r["category"] for r in relative_trueness],
        )
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
                "method_comparison_by_category": extract_method_comparison_by_category(pdf_path),
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
            "(quantitative) and method_comparison_by_category (qualitative) are extracted from "
            "the report's per-category results table via its structural cell grid (pdfplumber), "
            "not by matching the table header's exact wording -- confirmed necessary since real "
            "reports from different expert laboratories phrase that header differently. "
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
