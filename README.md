# Microbiology Methods

A database and dynamic infographic of validated microbiology analysis methods
(microorganism × matrix), sourced from the three main validation bodies:
**AFNOR NF-Validation**, **MicroVal**, and **AOAC Performance Tested Methods℠ (AOAC-RI)**.

Goal: browse which validated methods exist for a given micro-organism/matrix
combination, then drill into performance data (LOD50, discordance, inclusivity/
exclusivity, etc.) extracted from the underlying validation reports.

## Architecture

- **Data layer** — structured JSON, versioned in this repo:
  - `data/<source>/` and `data/<source>_<collector>/` — raw, per-collector
    output, one file per scraped record, unmodified extraction.
  - `data/methods/` — canonical layer, one file per real-world method,
    produced by `pipeline/` scripts that merge/reconcile the raw collectors
    and validate against `schema/method.schema.json`. This is what a
    frontend should read.
  - No external database: git history *is* the change history of the
    certification landscape.
- **Mining agents** — one module per source under `scrapers/`, because the
  three bodies publish very differently:
  - NF-Validation publishes both a certificate-list PDF and, per target
    organism, a live web page linking straight to each certificate PDF and
    summary validation report. `scrapers/summary_report_parser.py` mines
    performance data (relative trueness, accuracy-profile acceptability
    limit, inclusivity/exclusivity) out of those summary reports.
  - MicroVal's public page is a shell that loads its real certificate table
    from an iframe (`nen.bettywebblocks.com/view-microval`) — not yet scraped.
  - AOAC-RI certificates are parsed by `scrapers/aoac_ptm_parser.py` from
    manually-supplied certificate PDFs (a live crawl of members.aoac.org
    hasn't been built yet).
- **Normalization** — `pipeline/normalize_nf_validation.py` reconciles the
  NF-Validation collectors into `data/methods/`; `pipeline/normalize_aoac.py`
  does the equivalent (straight transform, no reconciliation needed yet) for
  AOAC-RI.
- **Orchestration** — `.github/workflows/scrape_and_normalize.yml` runs
  weekly (and on manual dispatch) from a normal-egress GitHub runner: re-fetch
  the live NF-Validation organism pages, re-run the merge pipeline, fetch and
  mine summary reports, and open a PR with any changes. Its live-fetch code
  paths were developed and only tested offline (this repo's own dev
  environment can't reach nf-validation.afnor.org) — expect to need small
  fixes on its first real runs.
- **Frontend** (planned) — a static site (GitHub Pages) reading an aggregated
  `data.json` built from `data/methods/`: a micro-organism × matrix heatmap of
  available methods, drilling into per-method detail pages.

## Data provenance policy

**Every record is tagged with where it came from and how fresh that source is
known to be.** The NF-Validation certificate *list* PDF bootstrap
(`data/nf_validation/`, `provenance.source_type: "bootstrap_pdf_import"`) was a
one-time seed from a manually-supplied file, not a live scrape — its own
refresh cadence relative to the live site was unknown. That gap is now closed:
`scrapers/nf_validation_organism_pages.py` reads the live
`nf-validation.afnor.org/domaine-agroalimentaire/<organism>/` pages directly,
each of which states its own last-updated date
(`traceability.organism_page_last_updated_raw`) and links straight to the
certificate + summary-report PDFs. The two collectors are reconciled by
`pipeline/normalize_nf_validation.py` (see below) rather than one superseding
the other outright, since each carries information the other lacks.

## Repository layout

```
schema/method.schema.json              canonical normalized method record (post-merge)
pipeline/normalize_nf_validation.py    merges the two NF-Validation collectors -> data/methods/
pipeline/normalize_aoac.py             transforms AOAC-RI raw records -> data/methods/
pipeline/fetch_and_mine_summary_reports.py  downloads + mines summary-report PDFs (live-egress only)
data/nf_validation/                    raw: bootstrap PDF-list import (138 certs)
data/nf_validation_organism_pages/     raw: live organism-page scrape (140 methods)
data/aoac_ptm/                         raw: AOAC-RI certificate parse (4 certs)
data/methods/                          canonical: merged, schema-valid (146 methods)
scrapers/                              one parser/scraper module per source
.github/workflows/                     scheduled scrape + normalize + PR
```

## Current status

- [x] `schema/method.schema.json` — canonical schema, informed by real fields
      observed across NF-Validation certificates, an AFNOR NF-Validation
      *summary validation report* (bias/accuracy-profile/LOQ/inclusivity data),
      four AOAC Performance Tested Methods℠ certificates, and the exact
      qualitative-vs-quantitative ISO 16140-2 performance vocabulary the
      project owner specified (sensitivity/RLOD/false-positive-ratio for
      qualitative methods; bias/SD-repeatability/β-ETI outliers for
      quantitative methods).
- [x] `scrapers/nf_validation_list_parser.py` — parses the AFNOR NF-Validation
      certificate-list PDF. 138 certificates bootstrapped.
- [x] `scrapers/nf_validation_organism_pages.py` — parses the live
      `nf-validation.afnor.org/domaine-agroalimentaire/<organism>/` pages.
      140 methods, direct links to certificate + summary-report PDFs for 135
      of them. Supports `--html-dir` (offline, developed/tested against
      this) and `--fetch-live` (crawls the real site — written for a
      normal-egress environment but not yet exercised there).
- [x] `pipeline/normalize_nf_validation.py` — reconciles the two NF-Validation
      collectors into `data/methods/` (142 unique certificates, 0 schema
      errors). Certificate-number matching with a normalized-commercial-name
      fallback (AFNOR sometimes re-uploads a certificate PDF under a filename
      date-stamped with the reissue date rather than the original certificate
      number, which broke naive filename-based matching for a handful of
      methods). Splits renewal vs. extension date history, computes
      active/expired status.
- [x] `scrapers/aoac_ptm_parser.py` + `pipeline/normalize_aoac.py` — parses
      AOAC-RI Performance Tested Methods certificates (which bundle
      certification metadata AND validation-study performance data in one
      PDF, unlike NF-Validation's separate cert + summary report) into
      `data/methods/`. Built and tested against the 4 example certificates
      available; inclusivity/exclusivity uses three fallback extraction
      strategies (narrative counts, a compact selectivity table, or counting
      per-strain table rows with discrepancy tracking) since real-world
      wording/layout varies even across just those 4. POD comparison tables
      (presumptive vs. confirmed, candidate vs. reference) are not mined —
      pypdf's plain-text extraction can't reliably recover their column
      layout. Coverage should be broadened once more certificates are
      reachable (a live AOAC-RI crawler hasn't been built — members.aoac.org
      needs a normal-egress environment, like the GitHub Actions workflow).
- [x] `scrapers/summary_report_parser.py` — mines performance data
      (relative-trueness-by-category, accuracy-profile acceptability limit
      and per-matrix SD repeatability, inclusivity/exclusivity with
      per-strain discrepancies) from an NF-Validation summary validation
      report PDF, auto-detecting the certificate number and method nature
      from the report's fixed cover page. Built and proven against the one
      real report available offline (TEMPO® EB / BIO 12/21-12/06) —
      including catching and fixing a real bug where naming
      `accuracy_profile.by_matrix` entries from the accuracy-profile chart
      captions silently swapped two matrices, found by rendering the actual
      PDF page to an image (via pymupdf) and checking the extracted text
      against it; matrices are now named by ISO 16140-2 food category
      instead, which held up under the same check.
      `accuracy_profile.by_matrix[].samples_out_of_beta_eti` and `loq_log`
      are deliberately left unmined — the former would need a
      product-to-category mapping that isn't safely extractable, and the
      latter's source table extracted as literal zeros — a wrong number is
      worse than a missing one. The inclusivity/exclusivity narrative
      regexes match this report's specific wording and will need
      broadening once run against reports phrased differently.
- [x] `.github/workflows/scrape_and_normalize.yml` — weekly (+ manual
      dispatch) job wiring the live-fetch scraper, the merge pipeline, and
      `pipeline/fetch_and_mine_summary_reports.py` together, opening a PR
      with any changes. Not yet validated against the real site (written
      from an environment that can't reach it) — watch its first runs.
- [ ] MicroVal scraper — needs the actual iframe content
      (`nen.bettywebblocks.com/view-microval`), not `microval.org` itself.
- [ ] Cross-source product grouping (NF-Validation × MicroVal × AOAC-RI by
      exact/near-exact commercial name) — no-op today since NF-Validation and
      AOAC-RI haven't produced any name collisions yet, and MicroVal isn't
      populated.
- [ ] Frontend: micro-organism × matrix heatmap + drill-down detail pages.

## Running the pipeline

```bash
pip install pypdf beautifulsoup4 jsonschema requests

# 1. Raw collectors
python3 scrapers/nf_validation_list_parser.py path/to/list.pdf --out-dir data/nf_validation           # one-time bootstrap seed only
python3 scrapers/nf_validation_organism_pages.py --html-dir path/to/saved/pages --out-dir data/nf_validation_organism_pages  # offline
python3 scrapers/nf_validation_organism_pages.py --fetch-live --out-dir data/nf_validation_organism_pages                    # live (normal-egress env)
python3 scrapers/aoac_ptm_parser.py --pdf-dir path/to/aoac/certs --out-dir data/aoac_ptm

# 2. Merge into the canonical layer
python3 pipeline/normalize_nf_validation.py
python3 pipeline/normalize_aoac.py

# 3. Mine performance data from summary-report PDFs (normal-egress env)
python3 pipeline/fetch_and_mine_summary_reports.py --skip-already-mined
# or mine a single already-downloaded report:
python3 scrapers/summary_report_parser.py --pdf path/to/report.pdf --methods-dir data/methods
```

## Notes on environment constraints

Live web scraping cannot run inside this interactive session — outbound HTTPS
here is proxy-restricted to a small allowlist. All scraper development so far
has been validated against manually-supplied source files (PDFs and saved
HTML pages), parsed locally with `pypdf`/`beautifulsoup4`, no rendering/OCR
dependency required. The scheduled scraping job needs a normal-egress
environment (GitHub Actions is the natural fit) to fetch these sources live
and re-run the same parsers unchanged.
