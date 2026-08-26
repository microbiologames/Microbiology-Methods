# Microbiology Methods

A database and dynamic infographic of validated microbiology analysis methods
(microorganism × matrix). Current scope: **ISO 16140-2 validated methods**,
i.e. **AFNOR NF-Validation** and **MicroVal**. **AOAC Performance Tested
Methods℠ (AOAC-RI)** is deliberately excluded from the frontend for now — its
scraper is confirmed broken on AOAC's own side (see the AOAC-RI section
below), and building out the tool for the two working, ISO 16140-2-aligned
sources first is a large enough scope on its own. The data pipeline and code
for AOAC-RI stay in the repo; revisit its exclusion once that scraper is
picked back up.

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
    and reads the real DataTables-rendered table directly (confirmed
    real, not scraped-and-guessed, against two actual runs).
  - AOAC-RI certificates are parsed by `scrapers/aoac_ptm_parser.py`
    (manually-supplied PDFs) or `scrapers/aoac_ptm_live_fetch.py` (crawls
    members.aoac.org's listing page, downloads each certificate PDF, and
    reuses the same parser).
- **Normalization** — `pipeline/normalize_nf_validation.py` reconciles the
  NF-Validation collectors into `data/methods/`; `pipeline/normalize_aoac.py`
  and `pipeline/normalize_microval.py` do the equivalent (straight
  transform, no reconciliation needed yet) for AOAC-RI and MicroVal.
- **Orchestration** — one GitHub Actions workflow per source
  (`.github/workflows/scrape_afnor.yml`, `scrape_microval.yml`,
  `scrape_aoac.yml`), daily + manual dispatch, from normal-egress runners.
  Split by source deliberately, not bundled into one job: AFNOR's
  summary-report mining step alone takes ~40 minutes, so bundling meant
  every debugging cycle on the (currently broken) AOAC-RI scraper paid that
  cost for nothing. Each opens its own PR (`automated/<source>-scrape`) and
  uploads a debug artifact (raw HTML/screenshots/JSON) so failures are
  diagnosable without another manual file handoff.
- **Frontend** (`web/`) — a dependency-free static page reading `web/data.json`
  (built from `data/methods/` by `pipeline/build_frontend_data.py`): an
  organism × category heatmap, toggling between method category and mined
  tested-food-category, drilling into per-method detail pages. Currently
  174 methods (142 NF-Validation + 32 MicroVal) — `build_frontend_data.py`
  excludes AOAC-RI records (`EXCLUDED_SOURCES`), matching the project's
  current ISO 16140-2-only scope. Deployed to GitHub Pages at
  **https://microbiologames.github.io/Microbiology-Methods/** —
  `.github/workflows/deploy_pages.yml` republishes `web/` automatically on
  every push to `main` that changes it, which in practice means every
  merged scrape PR (each ends with a "Rebuild frontend data" step), so the
  live page tracks whatever's actually on `main` with no manual publish
  step. See the status section below for why "matrix" needed a different
  data source than originally planned.

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
data/microval/                         raw: live-fetched, per-certificate (34 certs)
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
      listing page and feeds any discovered certificate PDFs through
      `aoac_ptm_parser.parse_certificate()`. A plain HTTP GET always finds 0
      PDF links (real mechanism: a hidden-field + postback download button,
      not `<a href="*.pdf">` links), so it falls back to headless Chromium —
      selecting "Microbiological" in the page's own Discipline filter (a
      real, DOM-verified selection, and this project's actual scope) and
      clicking its "Find" button, mirroring `microval_live_fetch.py`'s
      approach. Across several real CI runs this narrowed all the way down
      to a definitive root cause on AOAC's own side — see the "Real runs so
      far" note above for the full story — captured via console-error/
      pageerror hooks and a direct `Page_Validators`/`__doPostBack`
      inspection, not guessed.
- [x] `scrapers/microval_live_fetch.py` + `pipeline/normalize_microval.py` —
      live fetch (headless Chromium, since the real content loads into an
      iframe — `nen.bettywebblocks.com/view-microval` and
      `/view-microval-confirmation` — the same way `microval.org` itself
      turned out to be client-rendered) plus a real normalizer into
      `data/methods/`. Started as pure reconnaissance (no sample of the real
      content had ever been available), but its first two real CI runs
      settled the open questions: the one JSON network response seen on
      both pages is just the jQuery DataTables plugin's i18n string file,
      not certificate data — but the actual table is genuine server-rendered
      markup (DataTables always progressively-enhances a real `<table>`),
      with a consistent 6-column layout confirmed identical on both pages:
      Analyte, Certificate number, Test kit name, Supplier - manufacturer,
      Expiry date, Status. 32 real certificates merged into `data/methods/`.
      Cell values are read per-`<td>` rather than joined into one string and
      split back apart, since the test-kit-name and supplier fields are both
      free multi-word text with no fixed boundary between them. One real bug
      found from the merged data itself: `get_text(strip=True)` (no
      separator) silently drops the space when a cell wraps across a `<br>`
      or nested span, producing near-duplicate organisms in the frontend
      heatmap ("Bacillus cereus group" vs "Bacillus cereusgroup",
      "Cronobacter spp." vs "Cronobacterspp.", etc.) — fixed with an explicit
      `get_text(" ", strip=True)` + whitespace-collapse helper, re-scraped,
      re-merged.
- [x] `.github/workflows/scrape_afnor.yml` / `scrape_microval.yml` /
      `scrape_aoac.yml` — one workflow per source, daily + manual dispatch,
      each opening its own PR. Originally one combined workflow; split once
      it became clear that bundling made iterating on any single source
      expensive — AFNOR's summary-report mining step alone takes ~40
      minutes, so every AOAC-RI debugging cycle was paying that cost for
      nothing. Live-fetch steps use `continue-on-error: true` so a failure
      doesn't block normalization/frontend-rebuild steps downstream, and a
      debug-dump artifact (raw HTML/screenshots/JSON) uploads on every run —
      including failed ones — so a failure is diagnosable from the Actions
      UI without another manual file handoff.

      **Real runs so far (2026-08-25):** NF-Validation live-fetch has worked
      correctly from the first run — 142 methods across all 17 organism
      pages, merge succeeded with 0 schema errors every time. Summary-report
      mining went 1 → 64 → 93 (of 140) reports merged across runs as real
      failures got fixed: a missing `cryptography` dependency crashed the
      whole batch on the first AES-encrypted PDF (now installed, and each
      PDF is wrapped in its own try/except so one bad report can't stop the
      rest), and the certificate-number regex only recognized the one
      English label TEMPO EB happened to use (widened to accept French
      variants plus a format-only fallback — 47 reports still don't match
      and haven't been individually diagnosed yet).

      **"Merged" isn't the same as "has a real category breakdown"**,
      and checking that distinction (prompted by a direct question about why
      the frontend still looked sparse) found a much bigger gap than the
      merge count implies: of the 91 NF-Validation records with a populated
      `performance` field, only **9** actually have a non-empty
      category-level breakdown. The other 82 split into two causes, not one:
      (1) **qualitative reports (58 records — VIDAS, MicroSEQ, molecular
      kits) get zero, unconditionally** —
      `summary_report_parser.py`'s `mine_performance()` hardcodes
      `qualitative.method_comparison_by_category` to `[]` with no extraction
      attempt at all, a gap that existed from the start, not a regression;
      (2) **quantitative reports only succeed 9/33** —
      `extract_relative_trueness_by_category()`'s header regex was built
      and tested against exactly one report (TEMPO EB) and most others
      evidently phrase that table differently.

      **Fix: rewrote category-breakdown extraction on pdfplumber's
      structural table extraction instead of text regexes.** A temporary
      `debug_reports.yml` workflow (deleted once this was done) dumped
      pdfplumber's real cell grid for two real reports (one qualitative,
      one quantitative), which showed why the regex approach couldn't work:
      each expert laboratory phrases the same conceptual table header
      differently, and one report renders its "D-bar (bias)" symbol as
      garbled Unicode glyphs no text regex would ever match — while the
      actual cell structure (which column holds "SD", which holds "95%
      lower limit") stayed reliably extractable regardless.
      `find_tables_by_header()` now locates the right table by its cell
      contents' keywords rather than exact wording, and
      `summary_report_parser.py` handles three real per-category row shapes
      confirmed against actual reports: flat (one row per category),
      hierarchical-with-a-Total-row (a label-only category row followed by
      a/b/c sub-items and an aggregate "Total" row), and — found only in
      the qualitative table — a category row whose first sub-item's data
      shares the same row as the category id/name, which doesn't line up
      column-for-column with the header the way the other two shapes do
      and is instead read by column position counted back from the end of
      the row. `method_comparison_by_category` (qualitative) is
      implemented for the first time as part of this rewrite. Verified
      locally against both real captured table structures via a
      monkey-patched `pdfplumber.open`; a full re-mine of all 142 records
      (`workflow_dispatch` with `force_remine: true`, since the default
      `--skip-already-mined` would otherwise skip every record that
      already has a — possibly empty — `performance` field) is the next
      step to confirm the real improvement in category-breakdown coverage
      against the 9/91 baseline above.
      MicroVal went from
      reconnaissance to a real working collector, as described above. PR
      creation itself failed on the first run
      (`GitHub Actions is not permitted to create or approve pull requests`,
      the repo's Settings → Actions → General → Workflow permissions
      toggle, off by default) but has worked since that was enabled. The
      first real MicroVal collector run actually hit a *different* PR-push
      failure specific to that run (a GitHub App token lacking the
      `workflows` permission scope needed because that particular diff
      touched a workflow file mid-refactor — unrelated to the toggle above)
      and its data was lost with it; that stale PR was closed and the
      workflow re-run cleanly once the split was complete, landing the real
      32 certificates on `main`. Repo hygiene: per-run debug dumps (raw
      HTML/screenshots/JSON) were briefly getting committed to git history
      alongside the real data changes — fixed by `.gitignore`-ing `debug/`
      and restricting `peter-evans/create-pull-request`'s `add-paths` to
      `data/` and `web/`, since the debug dump is already uploaded as its
      own workflow artifact.
      AOAC-RI is now **confirmed broken on AOAC's own side**, not a scraper
      gap — a Playwright-driven rewrite was built (selecting a real filter,
      Discipline=Microbiological, and clicking the page's own "Find"
      button), but the search never reaches the server on any attempt.
      The listing page (an ASP.NET/iMIS site under `members.aoac.org`) is a
      genuine, working-looking search widget — not login-gated, not
      empty-by-design — but its `__doPostBack` function is provably
      undefined at click time (confirmed with an explicit 10-second wait,
      ruling out a load-order race) because the page's own
      `Telerik.Web.UI.RadAjaxManager._applyUpdatePanelsRenderMode` throws
      `Cannot read properties of null (reading 'length')` during
      `Sys.Application._doInitialize()`, aborting the AJAX framework's
      client-side init before it finishes wiring up postback support. That
      stack trace was captured directly, not inferred, and points at a
      server-side RadAjaxManager misconfiguration (likely referencing an
      UpdatePanel/container that doesn't exist in this page's current
      markup) that would break the "Find" button for any real browser —
      not something browser automation can route around. The scraper still
      attempts the full flow every run and captures console/page errors,
      so this will self-correct automatically the day AOAC fixes their
      page; no further scraper iteration is expected to help until then.
- [ ] Cross-source product grouping (NF-Validation × MicroVal by
      exact/near-exact commercial name) — no-op today since no name
      collisions exist yet across the two now-populated, in-scope sources.
- [x] Frontend (`web/`) — a dependency-free static page reading
      `web/data.json` (built by `pipeline/build_frontend_data.py` from
      `data/methods/`, excluding AOAC-RI records per the project's current
      ISO 16140-2-only scope — see the top of this README): an organism ×
      category heatmap with a toggle between two distinct axes, since they
      come from different fields entirely — **method category** (detection
      technology: culture media / PCR / immunological / …, well-populated
      everywhere) and **tested food category** (the categories actually
      exercised in the validation study, from mined
      `performance.*.{method_comparison_by_category,
      relative_trueness_by_category}`, falling back to a certificate's own
      `validation_scope.matrices` when nothing's been mined yet — see the
      note below on why the certificate's own scope isn't usable directly).
      Clicking a cell lists matching methods; clicking a method opens a
      detail view with the full record, including mined performance tables
      when present. Clicking a cell also scrolls the results list into
      view and briefly highlights it — added after the project owner
      pointed out that the results update wasn't otherwise noticeable once
      the heatmap has enough rows to push it below the fold. Status filter
      (valid-only by default), a source filter (NF-Validation / MicroVal),
      a manufacturer dropdown, and a name/organism search, all composable.
      A mining-progress bar (methods with `has_performance_data` / total,
      for whatever's currently filtered in) sits above the heatmap, always
      visible — a direct answer to "is the mining actually working",
      requested after the AFNOR re-mine landed and the frontend still
      needed a manual refresh to reflect it. Currently 174 methods (142
      NF-Validation + 32 MicroVal). Verified end-to-end in a real browser
      (Playwright) —
      this caught and fixed two real bugs during initial development (a
      missing parenthesis crashing the whole page, and a hidden overlay
      that still intercepted clicks because its CSS unconditionally set
      `display: flex` without an `[hidden]` override), and again after
      merging the real MicroVal data (confirmed no duplicate-organism rows
      after the cell-text-extraction fix above, and the full cell-click →
      result-list → detail-overlay flow with real records from both
      sources).

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
      answer lives in the mined validation-study data, shown honestly (a
      "Not yet mined" bucket) for the records that aren't there yet.

      **The tested-food-category axis is normalized onto ISO 16140-2:2016
      Annex A's own fixed 18-category taxonomy** (`pipeline/
      food_categories.py`), not left as raw mined text — prompted directly
      by the project owner reviewing the heatmap and finding it "pas
      propre du tout": real reports turned out to use ~108 distinct
      free-text category strings for what's really at most 18 categories
      ("Dairy products" / "Milk & Dairy products" / "Raw dairy products" /
      "Raw milk and dairy products" / ... all the same category), making
      the axis reshuffle unpredictably as more reports got mined. The 18
      categories and their exact English/French names come from the
      project owner's own copy of Annex A's Table A.1 (both language
      editions, transcribed into `pipeline/food_categories.py`, not
      reconstructed from memory), and `normalize_food_category()` keyword-
      matches each raw label onto one of them: 232/244 real category
      mentions (95%) resolved automatically against the current mined
      data, and the remaining 12 (bare labels naming no recognizable food
      family, e.g. "Miscellaneous", or a truncated extraction artifact
      like a lone "Production") are left out and logged rather than
      guessed. Where a raw label doesn't state ISO 16140-2's raw/
      ready-to-eat split explicitly (most don't — reports often just say
      "Meat products"), it defaults to that family's raw/unprocessed Annex
      A category, a documented assumption, not a certainty. The heatmap's
      tested-food-category axis now always shows all 18 Annex A columns
      (plus "Not yet mined"), even ones with zero methods yet — a stable,
      comparable column set instead of one that only shows whatever
      happens to have data today.

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
python3 scrapers/microval_live_fetch.py --out-dir data/microval --debug-dir /tmp/microval_debug      # live (normal-egress env)

# 2. Merge into the canonical layer
python3 pipeline/normalize_nf_validation.py
python3 pipeline/normalize_aoac.py
python3 pipeline/normalize_microval.py

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
