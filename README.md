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
    summary validation report.
  - MicroVal's public page is a shell that loads its real certificate table
    from an iframe (`nen.bettywebblocks.com/view-microval`) — not yet scraped.
  - AOAC-RI requires querying an online search tool; results are often partial
    (a certificate PDF plus, sometimes, a fuller validation summary). Not yet
    scraped.
- **Normalization** — `pipeline/normalize_nf_validation.py` reconciles the
  NF-Validation collectors (see below) into `data/methods/`.
- **Orchestration** (planned) — a scheduled GitHub Action runs the scrapers,
  the normalization pipeline, and opens a PR with new/changed data for review
  before merge.
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
schema/method.schema.json          canonical normalized method record (post-merge)
pipeline/normalize_nf_validation.py  merges the two NF-Validation collectors -> data/methods/
data/nf_validation/                 raw: bootstrap PDF-list import (138 certs)
data/nf_validation_organism_pages/  raw: live organism-page scrape (140 methods)
data/methods/                       canonical: merged, schema-valid (145 methods)
scrapers/                           one parser/scraper module per source
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
      of them. Offline mode only so far (reads saved HTML) — a `--fetch-live`
      crawl mode needs a normal-egress environment.
- [x] `pipeline/normalize_nf_validation.py` — reconciles the two NF-Validation
      collectors into `data/methods/` (145 unique certificates, 0 schema
      errors). Certificate-number matching with a normalized-commercial-name
      fallback (AFNOR sometimes re-uploads a certificate PDF under a filename
      date-stamped with the reissue date rather than the original certificate
      number, which broke naive filename-based matching for a handful of
      methods). Splits renewal vs. extension date history, computes
      active/expired status.
- [ ] MicroVal scraper — needs the actual iframe content
      (`nen.bettywebblocks.com/view-microval`), not `microval.org` itself.
- [ ] AOAC-RI scraper/parser (structure differs per certificate; no site
      access from this environment — needs to run somewhere with normal
      internet egress, e.g. GitHub Actions).
- [ ] Cross-source product grouping (NF-Validation × MicroVal × AOAC-RI by
      exact/near-exact commercial name) — no-op today since only one source
      is populated.
- [ ] Detailed validation-report mining (performance data: bias, accuracy
      profile, LOQ, inclusivity/exclusivity) — proven feasible against the
      TEMPO® EB example report and schema-modeled, not yet generalized into a
      parser. 135 methods already have a direct summary-report PDF URL ready
      to mine.
- [ ] GitHub Actions workflow (scheduled run → schema validation → PR).
- [ ] Frontend: micro-organism × matrix heatmap + drill-down detail pages.

## Running the pipeline

```bash
pip install pypdf beautifulsoup4 jsonschema

# 1. Raw collectors (offline mode: reads locally-saved source files)
python3 scrapers/nf_validation_list_parser.py path/to/list.pdf --out-dir data/nf_validation
python3 scrapers/nf_validation_organism_pages.py --html-dir path/to/saved/pages --out-dir data/nf_validation_organism_pages

# 2. Merge into the canonical layer
python3 pipeline/normalize_nf_validation.py
```

## Notes on environment constraints

Live web scraping cannot run inside this interactive session — outbound HTTPS
here is proxy-restricted to a small allowlist. All scraper development so far
has been validated against manually-supplied source files (PDFs and saved
HTML pages), parsed locally with `pypdf`/`beautifulsoup4`, no rendering/OCR
dependency required. The scheduled scraping job needs a normal-egress
environment (GitHub Actions is the natural fit) to fetch these sources live
and re-run the same parsers unchanged.
