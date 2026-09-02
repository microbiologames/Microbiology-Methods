"""Backfill performance data into data/methods/ records the deterministic
pdfplumber pipeline couldn't mine, using scrapers/llm_report_miner.py.

Targets exactly the records that have a summary_report_pdf_url but NO real
per-category breakdown (91 of them as of writing; a further 5 have no
report URL at all and are unreachable by any miner). Records the
deterministic pipeline already mined are never touched -- that data is
free, deterministic and auditable, and there's no reason to pay to
re-derive it or risk replacing it with a less certain extraction.

Every record written here is marked traceability.extraction_confidence =
"medium" (vs. "high" for the deterministic path) so LLM-extracted numbers
stay distinguishable in the data forever, and the model's own
extraction_notes are appended to traceability.notes -- on the real
calibration sample those notes surfaced genuine source-document
inconsistencies (two conflicting bias columns; per-category n disagreeing
between tables), so they're worth keeping rather than discarding.

Calibration status before this was trusted (see validate_llm_miner.py and
.github/workflows/calibrate_llm_miner.yml): on 5 known-good reports, the
LLM path reproduced the deterministic pipeline's numbers exactly --
every compared field within +/-0.05.

Cost control: --limit caps how many records are processed in one run
(each is one paid API call), and --skip-existing means an interrupted or
partial run can simply be re-run to continue where it left off.

Usage:
    python3 backfill_llm_performance.py --methods-dir ../data/methods --limit 25
"""
import argparse
import json
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import anthropic
import jsonschema
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from llm_report_miner import (
    MAX_PAGES_SENT, build_request_params, make_client, mine_with_llm, parse_response,
)


def has_real_breakdown(record: dict) -> bool:
    perf = record.get("performance")
    if not perf:
        return False
    nature = perf.get("method_nature")
    rows = (perf.get("quantitative", {}).get("relative_trueness_by_category")
            if nature == "quantitative" else perf.get("qualitative", {}).get("method_comparison_by_category"))
    return bool(rows)


def find_targets(methods_dir: Path, skip_existing: bool = True):
    """(path, record, pdf_url) for every NF-Validation record that has a
    summary report to mine but no real per-category breakdown yet."""
    targets = []
    for f in sorted(methods_dir.glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        if record.get("source") == "MICROVAL":
            # MicroVal reports aren't wired into this miner yet -- its
            # summary_report_pdf_url points at a differently-structured
            # document this prompt hasn't been calibrated against.
            continue
        if skip_existing and has_real_breakdown(record):
            continue
        pdf_url = record.get("traceability", {}).get("summary_report_pdf_url")
        if not pdf_url:
            continue
        targets.append((f, record, pdf_url))
    return targets


def merge_mined(record: dict, mined: dict) -> dict:
    record["performance"] = mined["performance"]
    tr = record.setdefault("traceability", {})
    # "medium", never "high": this is a real, calibrated extraction but it
    # is not the deterministic path, and that distinction should survive in
    # the data rather than living only in a commit message.
    tr["extraction_confidence"] = "medium"
    notes = (tr.get("notes") or "").strip()
    addition = mined["mining_notes"]
    if addition and addition not in notes:
        tr["notes"] = f"{notes} {addition}".strip()
    return record


def apply_mined(path: Path, record: dict, mined: dict, validator, cert: str) -> str:
    """Validate one extraction and write it. Returns the outcome bucket.

    Shared by the synchronous and batch paths so a record is accepted on
    exactly the same terms either way -- the schema check is the last thing
    standing between a model's output and the published catalogue, and it
    should not exist in two versions.
    """
    if not mined["performance"] or not has_real_breakdown({"performance": mined["performance"]}):
        # Confirmed real failure mode during calibration: a response that
        # parses fine but carries an empty category array. Left untouched
        # (not written as an empty result) so a later run can retry it
        # rather than it looking permanently mined-but-empty.
        print(f"[{cert}] no usable breakdown returned; leaving record untouched "
              f"(notes={mined.get('extraction_notes')!r})", file=sys.stderr)
        return "no_data"

    candidate = merge_mined(json.loads(json.dumps(record)), mined)
    errors = list(validator.iter_errors(candidate))
    if errors:
        print(f"[{cert}] SCHEMA ERROR, not written: {errors[0].message}", file=sys.stderr)
        return "invalid"

    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = (candidate["performance"].get("quantitative", {}).get("relative_trueness_by_category")
            or candidate["performance"].get("qualitative", {}).get("method_comparison_by_category"))
    print(f"[{cert}] wrote {len(rows)} category row(s)", file=sys.stderr)
    return "written"


_CUSTOM_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def custom_id_for(path: Path) -> str:
    """A batch custom_id derived from the record's filename.

    The API accepts only [A-Za-z0-9_-], and results come back in ANY order,
    so this is the only thing tying a response to the record it belongs to.
    Derived from the filename rather than a counter precisely so a resumed
    run maps results correctly without needing the original run's list.
    """
    return _CUSTOM_ID_RE.sub("-", path.stem)[:64]


def submit_batch(client, targets, max_pages):
    """Download each report, build its request, send the lot as one batch."""
    requests, index = [], {}
    for path, record, pdf_url in targets:
        cert = record["source_certificate_number"]
        cid = custom_id_for(path)
        if cid in index:
            print(f"[{cert}] SKIPPED: custom_id {cid!r} collides with "
                  f"{index[cid][1]['source_certificate_number']}", file=sys.stderr)
            continue
        print(f"[{cert}] preparing {pdf_url}", file=sys.stderr)
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                urllib.request.urlretrieve(pdf_url, tmp.name)
                params = build_request_params(Path(tmp.name), max_pages)
        except Exception as exc:  # noqa: BLE001 -- one bad report must not sink the batch
            print(f"[{cert}] ERROR preparing: {exc}", file=sys.stderr)
            continue
        index[cid] = (path, record)
        requests.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params)))

    if not requests:
        raise RuntimeError("nothing to submit")

    batch = client.messages.batches.create(requests=requests)
    return batch.id, index


def wait_for_batch(client, batch_id: str, poll_seconds: int, max_wait_seconds: int) -> bool:
    """Poll until the batch ends. False on timeout -- the caller must NOT
    treat that as failure: the batch is paid for and still running, and the
    results stay retrievable with --batch-id."""
    waited = 0
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return True
        if waited >= max_wait_seconds:
            return False
        print(f"  batch {batch_id}: {batch.processing_status} "
              f"({batch.request_counts.processing} processing, "
              f"{batch.request_counts.succeeded} succeeded) -- waited {waited}s",
              file=sys.stderr)
        time.sleep(poll_seconds)
        waited += poll_seconds


# Batch pricing is 50% of standard. Sonnet 5 standard is $2/$10 per MTok
# (claude-api skill, cached 2026-06-24), so batched it is $1/$5. Hard-coded
# on purpose: this is a reporting figure for the run log, and a wrong number
# in a log is better than a silent failure fetching a price list mid-run.
_BATCH_USD_PER_MTOK_IN = 1.00
_BATCH_USD_PER_MTOK_OUT = 5.00


def run_batch(client, batch_id: str, index: dict, validator) -> dict:
    """Apply a finished batch's results. Results arrive in any order, so
    every one is matched back by custom_id, never by position.

    Also totals the tokens actually billed. The whole point of running a
    small pilot batch is to replace an estimate with a measurement, and
    usage is only available here, per result -- there is no after-the-fact
    way to attribute a batch's cost to this job.
    """
    counts = {"written": 0, "no_data": 0, "invalid": 0, "failed": 0,
              "input_tokens": 0, "output_tokens": 0, "results": 0}
    for entry in client.messages.batches.results(batch_id):
        counts["results"] += 1
        usage = getattr(getattr(entry.result, "message", None), "usage", None)
        if usage is not None:
            counts["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            counts["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        target = index.get(entry.custom_id)
        if target is None:
            print(f"[{entry.custom_id}] result for an unknown record, ignored", file=sys.stderr)
            counts["failed"] += 1
            continue
        path, record = target
        cert = record["source_certificate_number"]

        if entry.result.type != "succeeded":
            detail = getattr(entry.result, "error", entry.result.type)
            print(f"[{cert}] batch entry {entry.result.type}: {detail}", file=sys.stderr)
            counts["failed"] += 1
            continue
        try:
            mined = parse_response(entry.result.message, path.name)
        except Exception as exc:  # noqa: BLE001
            print(f"[{cert}] ERROR parsing: {exc}", file=sys.stderr)
            counts["failed"] += 1
            continue
        counts[apply_mined(path, record, mined, validator, cert)] += 1
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods-dir", default="../data/methods")
    ap.add_argument("--schema", default="../schema/method.schema.json")
    ap.add_argument("--limit", type=int, default=25,
                    help="Max records to process this run -- each is one paid API call.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be processed and exit, without any API call.")
    ap.add_argument("--batch", action="store_true",
                    help="Send every record as one Message Batch instead of a call each. "
                         "Half price, no rate limits, whole backlog in one go -- at the cost "
                         "of being asynchronous.")
    ap.add_argument("--batch-id",
                    help="Resume: skip submission and apply the results of an existing batch. "
                         "A batch is paid for the moment it is submitted, so if the poll dies "
                         "this is how you collect what you already bought instead of paying twice.")
    ap.add_argument("--batch-id-file", default="last_batch_id.txt",
                    help="Where the submitted batch id is recorded, before any polling starts.")
    ap.add_argument("--cost-summary-file", default="batch_cost_summary.md",
                    help="Where the measured-cost table is written, for the PR body.")
    ap.add_argument("--poll-seconds", type=int, default=30)
    ap.add_argument("--max-wait-seconds", type=int, default=3600,
                    help="Stop polling after this long. The batch keeps running and stays "
                         "collectable with --batch-id; this only ends the waiting.")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Override how many pages of each report are sent (default: the "
                         "miner's MAX_PAGES_SENT).")
    args = ap.parse_args()

    methods_dir = Path(args.methods_dir)
    targets = find_targets(methods_dir)
    print(f"{len(targets)} record(s) still need a performance backfill; "
          f"processing up to {args.limit} this run.", file=sys.stderr)

    if args.dry_run:
        for path, record, pdf_url in targets[:args.limit]:
            print(f"  would mine {record['source_certificate_number']}: {pdf_url}", file=sys.stderr)
        return

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    mine_kwargs = {} if args.max_pages is None else {"max_pages": args.max_pages}

    if args.batch or args.batch_id:
        client = make_client()
        batch_targets = targets[:args.limit]

        if args.batch_id:
            batch_id = args.batch_id
            if batch_id == "latest":
                # Collecting an existing batch's results is a retrieval, not
                # a new inference: the batch was billed when it processed, so
                # this re-reads it for free. "latest" saves having to dig the
                # id out of a run artifact to do that.
                batch_id = next(iter(client.messages.batches.list(limit=1))).id
            # A resumed run rebuilds the mapping from the records on disk
            # rather than from the submitting run's memory, which is why
            # custom_id is derived from the filename and not a counter.
            index = {custom_id_for(path): (path, record)
                     for path, record, _ in find_targets(methods_dir, skip_existing=False)}
            print(f"Resuming batch {batch_id}", file=sys.stderr)
        else:
            batch_id, index = submit_batch(client, batch_targets,
                                           args.max_pages or MAX_PAGES_SENT)
            # Written before the first poll, deliberately: the batch is
            # already paid for at this point, and losing the id to a crashed
            # poll would mean paying for it twice.
            Path(args.batch_id_file).write_text(batch_id + "\n", encoding="utf-8")
            print(f"Submitted batch {batch_id} ({len(index)} request(s)); "
                  f"id saved to {args.batch_id_file}", file=sys.stderr)

        if not wait_for_batch(client, batch_id, args.poll_seconds, args.max_wait_seconds):
            print(f"\n=== Still running after {args.max_wait_seconds}s. Nothing is lost -- "
                  f"collect it later with:\n    --batch-id {batch_id} ===", file=sys.stderr)
            return

        counts = run_batch(client, batch_id, index, validator)
        remaining = len(find_targets(methods_dir))
        cost = (counts["input_tokens"] / 1e6 * _BATCH_USD_PER_MTOK_IN
                + counts["output_tokens"] / 1e6 * _BATCH_USD_PER_MTOK_OUT)
        n = max(counts["results"], 1)
        print(f"\n=== Batch {batch_id}: {counts['written']} written, "
              f"{counts['no_data']} returned no usable data, {counts['invalid']} schema-invalid, "
              f"{counts['failed']} errored; {remaining} record(s) still need backfilling ===",
              file=sys.stderr)
        summary = (
            f"**Measured cost of this batch**\n\n"
            f"| | |\n|---|---|\n"
            f"| Reports processed | {counts['results']} |\n"
            f"| Written | {counts['written']} |\n"
            f"| No usable breakdown | {counts['no_data']} |\n"
            f"| Schema-invalid | {counts['invalid']} |\n"
            f"| Errored | {counts['failed']} |\n"
            f"| Input tokens | {counts['input_tokens']:,} |\n"
            f"| Output tokens | {counts['output_tokens']:,} |\n"
            f"| **Cost** | **${cost:.4f}** (${cost / n:.4f} per report) |\n"
            f"| Remaining backlog | {remaining} records, "
            f"~${cost / n * remaining:.2f} at this rate |\n"
        )
        print("=== MEASURED COST ===\n" + summary, file=sys.stderr)
        # Also to a file: the run log's tail is dominated by git output and
        # this number is the whole reason for running a pilot batch, so it
        # goes somewhere it can be read without scrolling -- the pull
        # request body.
        Path(args.cost_summary_file).write_text(summary, encoding="utf-8")
        return

    written = failed = no_data = invalid = 0
    for path, record, pdf_url in targets[:args.limit]:
        cert = record["source_certificate_number"]
        print(f"[{cert}] mining {pdf_url}", file=sys.stderr)
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                urllib.request.urlretrieve(pdf_url, tmp.name)
                mined = mine_with_llm(Path(tmp.name), **mine_kwargs)
        except Exception as exc:  # noqa: BLE001 -- one bad report must not abort the batch
            print(f"[{cert}] ERROR: {exc}", file=sys.stderr)
            failed += 1
            continue

        outcome = apply_mined(path, record, mined, validator, cert)
        if outcome == "written":
            written += 1
        elif outcome == "no_data":
            no_data += 1
        else:
            invalid += 1

    remaining = len(targets) - written
    print(f"\n=== Backfill: {written} written, {no_data} returned no usable data, "
          f"{failed} errored, {invalid} schema-invalid; {remaining} record(s) still "
          f"need backfilling after this run ===", file=sys.stderr)


if __name__ == "__main__":
    main()
