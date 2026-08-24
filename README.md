# Microbiology Methods

A database and dynamic infographic of validated microbiology analysis methods
(microorganism × matrix), sourced from the three main validation bodies:
**AFNOR NF-Validation**, **MicroVal**, and **AOAC Performance Tested Methods℠ (AOAC-RI)**.

Goal: browse which validated methods exist for a given micro-organism/matrix
combination, then drill into performance data (LOD50, discordance, inclusivity/
exclusivity, etc.) extracted from the underlying validation reports.

## Architecture

- **Data layer** — structured JSON, one file per certified method, versioned in
  this repo under `data/<source>/`. No external database: git history *is* the
  change history of the certification landscape.
- **Mining agents** — one module per source under `scrapers/`, because the three
  bodies publish very differently:
  - NF-Validation / MicroVal publish detailed PDF validation reports and a
    certificate list — rich, structured, but PDF-based.
  - AOAC-RI requires querying an online search tool; results are often partial
    (a certificate PDF plus, sometimes, a fuller validation summary).
- **Orchestration** (planned) — a scheduled GitHub Action runs the scrapers,
  validates output against `schema/method.schema.json`, and opens a PR with
  new/changed data for review before merge.
- **Frontend** (planned) — a static site (GitHub Pages) reading an aggregated
  `data.json`: a micro-organism × matrix heatmap of available methods, drilling
  into per-method detail pages.

## Data provenance policy

**Every record is tagged with where it came from and how fresh that source is
known to be.** In particular: the NF-Validation certificate *list* currently in
`data/nf_validation/` was bootstrapped from a manually-supplied PDF
(`provenance.source_type: "bootstrap_pdf_import"`), not a live scrape. That
PDF's own refresh cadence relative to the live nf-validation.afnor.org site is
unknown, so it must **not** be treated as a recurring source of truth — it's a
one-time seed to validate the schema and pipeline. The production scraper
(`scrapers/nf_validation_list_parser.py` today parses a local PDF; a follow-up
`scrapers/nf_validation_live.py` needs to hit the live site) is what should run
on a schedule and supersede/reconcile these bootstrap records.

## Repository layout

```
schema/method.schema.json   canonical normalized method record (post-merge)
data/nf_validation/         one JSON per NF-Validation certificate (+ _index.json)
scrapers/                   one parser/scraper module per source
```

## Current status

- [x] `schema/method.schema.json` — canonical schema, informed by real fields
      observed across NF-Validation certificates, an AFNOR NF-Validation
      *summary validation report* (bias/accuracy-profile/LOQ/inclusivity data),
      and four AOAC Performance Tested Methods℠ certificates.
- [x] `scrapers/nf_validation_list_parser.py` — heuristic text-layout parser
      for the AFNOR NF-Validation certificate list PDF. Bootstrapped
      138/140 certificates into `data/nf_validation/*.json`. Extracts:
      certificate number, commercial name, target organism (keyword-matched),
      action (detection/enumeration), validation scope, certification/renewal/
      extension dates, company holder. **Not yet normalized to the canonical
      schema** — these are raw per-source records.
- [ ] Normalization pass: raw NF-Validation records → `method.schema.json`
      shape (matrix tagging, reference-method extraction, status computation).
- [ ] MicroVal scraper.
- [ ] AOAC-RI scraper/parser (structure differs per certificate; no site access
      from this environment — needs to run somewhere with normal internet
      egress, e.g. GitHub Actions).
- [ ] Detailed validation-report mining (performance data: bias, accuracy
      profile, LOQ, inclusivity/exclusivity) — proven feasible against the
      TEMPO® EB example report, not yet generalized into a parser.
- [ ] GitHub Actions workflow (scheduled run → schema validation → PR).
- [ ] Frontend: micro-organism × matrix heatmap + drill-down detail pages.

## Running the NF-Validation list parser

```bash
pip install pypdf
python3 scrapers/nf_validation_list_parser.py path/to/list.pdf --out-dir data/nf_validation
```

## Notes on environment constraints

Live web scraping cannot run inside this interactive session — outbound HTTPS
here is proxy-restricted to a small allowlist. All scraper development so far
has been validated against manually-supplied source PDFs (parsed locally with
`pypdf`, no rendering/OCR dependency required). The scheduled scraping job
needs a normal-egress environment (GitHub Actions is the natural fit).
