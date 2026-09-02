"""The Batch path, exercised end to end against a stub client.

A batch is paid for the moment it is submitted, and its results come back in
ANY order over an interface that is awkward to try out for real. So the two
things most likely to go wrong -- matching a result to the wrong record, and
losing a paid batch to a crashed poll -- are checked here rather than
discovered on a run that costs money.

Run: python3 tests/test_batch_path.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scrapers"))
import backfill_llm_performance as bf  # noqa: E402


class _ToolUse:
    type = "tool_use"
    def __init__(self, payload): self.input = payload


class _Message:
    stop_reason = "tool_use"
    def __init__(self, payload): self.content = [_ToolUse(payload)]


class _Result:
    def __init__(self, message=None, kind="succeeded", error=None):
        self.type, self.message, self.error = kind, message, error


class _Entry:
    def __init__(self, custom_id, result): self.custom_id, self.result = custom_id, result


class _Batches:
    def __init__(self, entries): self._entries = entries
    def results(self, batch_id): return iter(self._entries)


class _Client:
    def __init__(self, entries): self.messages = type("M", (), {"batches": _Batches(entries)})()


def qualitative_payload(cert, category):
    return {
        "certificate_number": cert,
        "method_nature": "qualitative",
        "quantitative": None,
        "qualitative": {"method_comparison_by_category": [{
            "category": category, "n_samples": 60,
            "positive_alternative": 30, "positive_reference": 30,
            "positive_deviation": 0, "negative_deviation": 0,
            "relative_trueness_percent": 100.0, "false_positive_ratio_percent": 0.0,
            "sensitivity_alternative_percent": 100.0, "sensitivity_reference_percent": 100.0,
        }]},
        "extraction_notes": None,
    }


class _AllowAll:
    def iter_errors(self, _candidate): return iter(())


def main() -> int:
    failures = []

    def check(label, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    # --- custom_id ---
    cid = bf.custom_id_for(Path("data/methods/nf_validation_3M_01-09_04.json"))
    check("custom_id keeps only characters the API accepts",
          all(c.isalnum() or c in "_-" for c in cid), cid)
    check("custom_id is stable across runs",
          cid == bf.custom_id_for(Path("data/methods/nf_validation_3M_01-09_04.json")))
    check("custom_id distinguishes two records",
          bf.custom_id_for(Path("a_1.json")) != bf.custom_id_for(Path("a_2.json")))
    check("custom_id fits the 64-char limit",
          len(bf.custom_id_for(Path("x" * 200 + ".json"))) <= 64)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        rec_a = {"id": "a", "source_certificate_number": "CERT-A", "traceability": {}}
        rec_b = {"id": "b", "source_certificate_number": "CERT-B", "traceability": {}}
        pa, pb = d / "rec_a.json", d / "rec_b.json"
        pa.write_text(json.dumps(rec_a), encoding="utf-8")
        pb.write_text(json.dumps(rec_b), encoding="utf-8")
        index = {bf.custom_id_for(pa): (pa, rec_a), bf.custom_id_for(pb): (pb, rec_b)}

        # Results deliberately reversed: the API makes no ordering promise,
        # and matching by position instead of custom_id would swap these two
        # records' performance data with no error raised anywhere.
        entries = [
            _Entry(bf.custom_id_for(pb), _Result(_Message(qualitative_payload("CERT-B", "Dairy products")))),
            _Entry(bf.custom_id_for(pa), _Result(_Message(qualitative_payload("CERT-A", "Raw meats")))),
        ]
        counts = bf.run_batch(_Client(entries), "batch_x", index, _AllowAll())
        check("both out-of-order results are written", counts["written"] == 2, str(counts))

        got_a = json.loads(pa.read_text(encoding="utf-8"))
        got_b = json.loads(pb.read_text(encoding="utf-8"))
        cat_a = got_a["performance"]["qualitative"]["method_comparison_by_category"][0]["category"]
        cat_b = got_b["performance"]["qualitative"]["method_comparison_by_category"][0]["category"]
        check("each record gets ITS OWN result, not its neighbour's",
              (cat_a, cat_b) == ("Raw meats", "Dairy products"), f"a={cat_a} b={cat_b}")
        check("the extraction is marked medium confidence, not high",
              got_a["traceability"]["extraction_confidence"] == "medium")

        # --- failure modes must not write anything ---
        pc = d / "rec_c.json"
        rec_c = {"id": "c", "source_certificate_number": "CERT-C", "traceability": {}}
        pc.write_text(json.dumps(rec_c), encoding="utf-8")
        idx_c = {bf.custom_id_for(pc): (pc, rec_c)}

        errored = [_Entry(bf.custom_id_for(pc), _Result(kind="errored", error="overloaded"))]
        counts = bf.run_batch(_Client(errored), "b", idx_c, _AllowAll())
        check("an errored entry counts as failed and writes nothing",
              counts["failed"] == 1 and "performance" not in json.loads(pc.read_text()), str(counts))

        empty = qualitative_payload("CERT-C", "x")
        empty["qualitative"]["method_comparison_by_category"] = []
        counts = bf.run_batch(_Client([_Entry(bf.custom_id_for(pc), _Result(_Message(empty)))]),
                              "b", idx_c, _AllowAll())
        check("an empty category array is left for a retry, not written as a result",
              counts["no_data"] == 1 and "performance" not in json.loads(pc.read_text()), str(counts))

        class _RejectAll:
            def iter_errors(self, _c):
                return iter([type("E", (), {"message": "nope"})()])
        counts = bf.run_batch(_Client([_Entry(bf.custom_id_for(pc), _Result(_Message(qualitative_payload("CERT-C", "Raw meats"))))]),
                              "b", idx_c, _RejectAll())
        check("a schema-invalid extraction never reaches the record",
              counts["invalid"] == 1 and "performance" not in json.loads(pc.read_text()), str(counts))

        orphan = [_Entry("no-such-record", _Result(_Message(qualitative_payload("CERT-Z", "Raw meats"))))]
        counts = bf.run_batch(_Client(orphan), "b", idx_c, _AllowAll())
        check("a result for an unknown record is reported, not applied somewhere",
              counts["failed"] == 1 and counts["written"] == 0, str(counts))

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===")
        for f in failures:
            print("  -", f)
        return 1
    print("=== all checks passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
