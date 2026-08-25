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
    from an iframe (`nen.bettywebblocks.com/view-microval`).
    `scrapers/microval_live_fetch.py` drives headless Chromium to render it
    and captures raw HTML/screenshots/JSON for a human to inspect — it's
    reconnaissance, not a finished collector, since no sample of the real
    content has ever been available to develop a real parser against.
  - AOAC-RI certificates are parsed by `scrapers/aoac_ptm_parser.py`
    (manually-supplied PDFs) or `scrapers/aoac_ptm_live_fetch.py` (crawls
    members.aoac.org's listing page, downloads each certificate PDF, and
    reuses the same parser).
- **Normalization** — `pipeline/normalize_nf_validation.py` reconciles the
  NF-Validation collectors into `data/methods/`; `pipeline/normalize_aoac.py`
  does the equivalent (straight transform, no reconciliation needed yet) for
  AOAC-RI. MicroVal has no normalization step yet (see status below).
- **Orchestration** — `.github/workflows/scrape_and_normalize.yml` runs
  weekly (and on manual dispatch) from a normal-egress GitHub runner: re-fetch
  the live NF-Validation organism pages, re-run the merge pipeline, mine
  summary reports, fetch AOAC-RI certificates live, run the MicroVal
  reconnaissance fetch, and open a PR with any changes — uploading a debug
  artifact (raw HTML/screenshots/JSON) on every run so failures are
  diagnosable. Every live-fetch code path was developed and only tested
  offline or against a local synthetic page (this repo's own dev environment
  can't reach any of the three real sites) — expect to need fixes on its
  first real runs, and check the debug artifact before assuming a source
  genuinely has no new data.
- **Frontend** (`web/`) — a dependency-free static page reading `web/data.json`
  (built from `data/methods/` by `pipeline/build_frontend_data.py`): an
  organism × category heatmap, toggling between method category and mined
  tested-food-category, drilling into per-method detail pages. Not yet
  deployed to GitHub Pages. See the status section below for why "matrix"
  needed a different data source than originally planned.

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
data/microval/                         raw: reconnaissance-only, unmapped (see status below)
data/methods/                          canonical: merged, schema-valid (146 methods)
scrapers/                              one parser/scraper module per source
pipeline/build_frontend_data.py        aggregates data/methods/ -> web/data.json
web/                                   static frontend (heatmap + drill-down), reads web/data.json
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
      reachable — see `scrapers/aoac_ptm_live_fetch.py` below.
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
- [x] `scrapers/aoac_ptm_live_fetch.py` — fetches the AOAC-RI validated-methods
      listing page, discovers certificate PDF links, and feeds each through
      `aoac_ptm_parser.parse_certificate()`. Detects (and logs, rather than
      silently returning nothing for) two likely failure modes: a login wall
      and a JS-rendered listing that a plain HTTP GET can't see. Pagination
      is only followed via a `rel="next"` link if present — an ASP.NET
      postback-based pager, if that's what the real site uses, isn't
      handled. Never run against the real site (members.aoac.org is
      egress-blocked from this repo's dev environment) — its very first
      real run, in CI, is also its first real test.
- [x] `scrapers/microval_live_fetch.py` — reconnaissance-grade live fetch for
      MicroVal using headless Chromium (Playwright), since the real content
      loads into an iframe (`nen.bettywebblocks.com/view-microval` and
      `/view-microval-confirmation`) that's almost certainly client-rendered
      the same way `microval.org` itself turned out to be. Captures the
      rendered HTML, a full-page screenshot, and the body of any JSON
      network responses seen while loading (in case the page fetches its
      data from a discoverable API, which would be far easier to parse than
      scraped HTML) — then makes one best-effort, explicitly-unmapped
      attempt to pull out repeated table/list rows as raw text. Verified
      end-to-end against a local synthetic test page (a JS app fetching a
      JSON API, rendering a table) — confirmed it captures the API response,
      the screenshot, and the rendered rows correctly — but this proves the
      *mechanism* works, not that MicroVal's real structure matches; no
      sample of MicroVal's actual rendered content has ever been available
      to develop against, only the outer shell page. Its output under
      `data/microval/` is intentionally left unmapped to the canonical
      schema — writing `pipeline/normalize_microval.py` needs a human to
      look at a real run's debug dump first.
- [x] `.github/workflows/scrape_and_normalize.yml` — weekly (+ manual
      dispatch) job wiring together the NF-Validation live-fetch scraper,
      the merge pipeline, the summary-report miner, the AOAC-RI live fetch,
      and the MicroVal reconnaissance fetch, opening a PR with any changes.
      Every live-fetch step uses `continue-on-error: true` so one source
      failing doesn't block the others, and a debug-dump artifact (raw
      HTML/screenshots/JSON) uploads on every run — including failed ones —
      so a failure is diagnosable from the Actions UI without another
      manual file handoff.

      **First real run (2026-08-25):** NF-Validation live-fetch worked
      correctly out of the box — 142 methods across all 17 organism pages,
      merge succeeded with 0 schema errors. Three real bugs surfaced and
      were fixed from that run's logs: (1) the summary-report miner crashed
      its whole batch on the first AES-encrypted PDF it hit (`cryptography`
      wasn't installed) — now installed, and each PDF is wrapped in its own
      try/except so one bad report can't stop the rest; (2) the AOAC-RI
      login-wall check false-positived on a normal persistent "Sign In" nav
      link and aborted before finding any certificates — narrowed to require
      an actual password field; (3) PR creation failed outright
      (`GitHub Actions is not permitted to create or approve pull requests`)
      — this is the repo's own Settings → Actions → General → Workflow
      permissions → "Allow GitHub Actions to create and approve pull
      requests" toggle, off by default; the workflow still pushes its commit
      to the `automated/validation-data-scrape` branch either way, but
      enable that setting for it to open the PR itself. On the encouraging
      side, the MicroVal reconnaissance fetch captured a real JSON API
      response from both `view-microval` and `view-microval-confirmation`
      on its very first try — the best-case outcome, meaning
      `pipeline/normalize_microval.py` can likely be written against a real
      API response rather than scraped HTML once that capture is reviewed.
- [ ] `pipeline/normalize_microval.py` — blocked on seeing a real run's
      output; the field layout (which column is the certificate number,
      product name, manufacturer…) is unknown until then.
- [ ] Cross-source product grouping (NF-Validation × MicroVal × AOAC-RI by
      exact/near-exact commercial name) — no-op today since NF-Validation and
      AOAC-RI haven't produced any name collisions yet, and MicroVal isn't
      normalized into `data/methods/` yet.
- [x] Frontend (`web/`) — a dependency-free static page reading
      `web/data.json` (built by `pipeline/build_frontend_data.py` from
      `data/methods/`): an organism × category heatmap with a toggle
      between two distinct axes, since they come from different fields
      entirely — **method category** (detection technology: culture media /
      PCR / immunological / …, well-populated everywhere) and **tested food
      category** (the categories actually exercised in the validation
      study, from mined `performance.*.{method_comparison_by_category,
      relative_trueness_by_category}`, falling back to a certificate's own
      `validation_scope.matrices` when nothing's been mined yet — see the
      note below on why the certificate's own scope isn't usable directly).
      Clicking a cell lists matching methods; clicking a method opens a
      detail view with the full record, including mined performance tables
      when present. Status filter (valid-only by default) and a
      name/organism search. Verified end-to-end in a real browser
      (Playwright) — this caught and fixed two real bugs (a missing
      parenthesis crashing the whole page, and a hidden overlay that still
      intercepted clicks because its CSS unconditionally set `display:
      flex` without an `[hidden]` override).

      **Why "tested food category" isn't just `validation_scope.matrices`:**
      per the project owner, ISO 16140-2 validations (NF-Validation,
      MicroVal) use a "Broad Range of Food" (BRF) rule — once a method is
      validated across 5+ food categories, its official scope becomes BRF
      regardless of which ones, which is exactly why `validation_scope.raw`
      collapses to some "all human food products" variant for 137/142
      NF-Validation certificates. AOAC-RI has no BRF concept, so its
      `validation_scope.matrices` genuinely lists the (narrower) tested
      matrices directly — that's why it's used as the fallback but not the
      primary source. The real "which categories were actually tested"
      answer lives in the mined validation-study data, which today only
      exists for the one hand-mined TEMPO EB report — the heatmap shows
      this honestly (a "Not yet mined" bucket) rather than pretending the
      certificate's BRF scope is a matrix breakdown.

      Also worth recording: building this surfaced a real pre-existing data
      bug (unrelated to tonight's live-fetch work) — 3 STEC certificates
      had `target_organism.normalized` set to raw French H1 text instead of
      the intended English label, splitting one organism into two heatmap
      rows. Root cause: `slug_from_filename_or_url`'s offline-fixture
      branch can slugify a descriptive local filename (e.g.
      `..._STEC_Shiga_Toxine_Escherichia_coli__NF_Validation.htm`) to
      something longer than `ORGANISM_SLUG_MAP`'s key (`stec`), missing an
      exact-match lookup that `--fetch-live` never has trouble with (it
      builds URLs directly from map keys). Fixed with a containment-based
      fallback that prefers the longest (most specific) matching key.

## Running the pipeline

```bash
pip install pypdf beautifulsoup4 jsonschema requests playwright cryptography
playwright install --with-deps chromium   # only needed for microval_live_fetch.py

# 1. Raw collectors
python3 scrapers/nf_validation_list_parser.py path/to/list.pdf --out-dir data/nf_validation           # one-time bootstrap seed only
python3 scrapers/nf_validation_organism_pages.py --html-dir path/to/saved/pages --out-dir data/nf_validation_organism_pages  # offline
python3 scrapers/nf_validation_organism_pages.py --fetch-live --out-dir data/nf_validation_organism_pages                    # live (normal-egress env)
python3 scrapers/aoac_ptm_parser.py --pdf-dir path/to/aoac/certs --out-dir data/aoac_ptm            # offline, from supplied PDFs
python3 scrapers/aoac_ptm_live_fetch.py --out-dir data/aoac_ptm --debug-dir /tmp/aoac_debug          # live (normal-egress env)
python3 scrapers/microval_live_fetch.py --out-dir data/microval --debug-dir /tmp/microval_debug      # reconnaissance only, see status above

# 2. Merge into the canonical layer
python3 pipeline/normalize_nf_validation.py
python3 pipeline/normalize_aoac.py

# 3. Mine performance data from summary-report PDFs (normal-egress env)
python3 pipeline/fetch_and_mine_summary_reports.py --skip-already-mined
# or mine a single already-downloaded report:
python3 scrapers/summary_report_parser.py --pdf path/to/report.pdf --methods-dir data/methods

# 4. Build and view the frontend
python3 pipeline/build_frontend_data.py
cd web && python3 -m http.server 8000   # fetch() needs http(s), not file://
```

## Notes on environment constraints

Live web scraping cannot run inside this interactive session — outbound HTTPS
here is proxy-restricted to a small allowlist. All scraper development so far
has been validated against manually-supplied source files (PDFs and saved
HTML pages), parsed locally with `pypdf`/`beautifulsoup4`, no rendering/OCR
dependency required. The scheduled scraping job needs a normal-egress
environment (GitHub Actions is the natural fit) to fetch these sources live
and re-run the same parsers unchanged.
