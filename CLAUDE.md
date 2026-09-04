# Microbiology Methods — working notes

A catalogue of ISO 16140-2 validated food-microbiology methods, scraped from
three certification schemes and published as a static GitHub Pages site.
Microbiologists use it to choose a validated method, so a wrong number here is
worse than a missing one. That single fact explains most of the design
decisions below.

## Shape of the thing

```
scrapers/   fetch + parse the certification bodies' pages and PDFs
pipeline/   normalize into data/methods/*.json, then build web/
data/       one JSON file per certificate (the source of truth)
web/        static site: index.html (performance) + catalog.html (certificates)
worker/     Cloudflare Worker: the catalogue's chat assistant
tests/      plain `python3 tests/<file>.py`, no framework, exit code is the result
```

Data flows one way: `scrapers/` → `data/methods/` → `pipeline/build_frontend_data.py`
→ `web/data.json` + `web/facets.json`. Never hand-edit `web/data.json`; rebuild it.

## Rules that matter

**Provenance is part of the data.** `traceability.extraction_confidence` is
`"high"` for the deterministic pdfplumber path and `"medium"` for anything an
LLM extracted. Keep that distinction; it is the reader's only signal.

**Canonicalize for display, never in the record.** `pipeline/taxonomy.py` folds
spelling/language/legal-entity variants (ADRIA = ADRIA Développement, Oxoid =
Thermo Fisher, "Levures et Moisissures" = "Yeasts and moulds") at build time.
Raw source values stay in `data/methods/`. Scope is never merged —
*Listeria* spp. is not *L. monocytogenes*.

**"Unknown" beats a confident guess.** `canonical_method_category` returns
`None` when nothing is known so callers can report it, and requires two
independent signals before classifying from report text. When a rule needs
inventing, read what the source document actually says first — there is a
free read-only workflow for that (`diagnose_unclassified_technology.yml`).
A wrong detection technology on a validated method misinforms a purchase.

**The catalogue page deliberately stops before performance data.** That was a
product decision, not an omission.

## Lore worth not rediscovering

- **The API key is identity-linked.** Every Anthropic call needs an
  `anthropic-workspace-id` header or it 400s. `scrapers/llm_report_miner.make_client()`
  sends it only when `ANTHROPIC_WORKSPACE_ID` is set (sending it with an
  ordinary key is itself an error). The value lives in GitHub Secrets and a
  Cloudflare Secret — **never commit it**.
- **A strict tool schema cannot pair an `enum` with a nullable type array.**
  Returns 400. Use `[]` / `""` as "no value", never `null`.
- **Native PDF input is billed per page** (text *and* a rendered image, roughly
  2,800 tokens/page). Sending whole 100-page reports is what made the first
  backfill cost ~$0.45 a record. `select_pages` sends the cover plus the pages
  that score for results tables: measured **$0.044/record batched**.
- **Batch API: paid at submission, results in any order.** Match by
  `custom_id` (derived from the filename, so a resumed run rebuilds the map
  from disk), and write the batch id to disk *before* polling. Re-collecting
  results is free — `--batch-id latest`.
- **`urllib` refuses non-ASCII URLs.** AFNOR names files `N°16_....pdf`.
  Always download through `scrapers/url_utils.encode_url`. This bug was fixed
  once per module and bit twice; `requests` escapes them itself, which is why
  one of the three download paths never showed the failure.
- **A scheduled scrape must not silently delete mined data.**
  `normalize_nf_validation.preserve_mined_fields` exists because rebuilding a
  canonical record drops `performance` and `expert_laboratory`; without it
  every weekly run wiped them.
- **Give long steps `timeout-minutes`.** `continue-on-error` handles a step
  that *fails*, not one that never finishes — a 1h38 hang once cancelled the
  job before it could open its PR, discarding the whole run.

## This environment

The agent proxy blocks `workers.dev`, `github.io`, the certification bodies'
PDF hosts, the Anthropic API, and the Azure blob store GitHub redirects job
logs to. **Anything needing the network runs in GitHub Actions**, not here.
Consequences worth planning around: job logs come back tail-only and are
swamped by the PR step's git output, so put results a human needs (measured
cost, counts) in the **pull request body**, not just stderr.

## Where it stands

238 records — 142 NF-Validation, 92 MicroVal, 4 AOAC-RI (excluded from the
site: AOAC's own listing is broken). 138 carry a per-category performance
breakdown, all NF-Validation. Every method has a named detection technology.

Open work, roughly in order of value:

1. **The AFNOR cover-page parser drops certificate numbers.** Every scheduled
   scrape therefore proposes ~35 phantom records with `source_certificate_number:
   null` that duplicate existing methods, and the PR has to be rejected by hand.
   Refusing to write a record without a certificate number would make the
   automated PRs trustworthy again; fixing the matching would be better.
2. **MicroVal has no performance data at all** (0/92). Its reports are a
   different format the miner has never been calibrated against.
3. Three NF-Validation records legitimately have no per-category table:
   Delvotest T and Premi Test screen for antibiotic residues, and
   BACGene GO Salmonella publishes a different document type.

## Conventions

- Branch: `claude/new-github-repo-5alcyv`. Never push to another branch.
- Verify before pushing: run every file in `tests/`, and rebuild
  `web/data.json` to confirm the published data matches the records.
- Automated data PRs are reviewed, not merged blind. A stale one may only be
  *added* from, never merged — it predates later work and would revert it.
- Commit messages explain *why*, especially the failure that motivated a
  guard. Several of the rules above survive only because a commit said so.
