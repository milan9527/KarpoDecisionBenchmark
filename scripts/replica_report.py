"""Verify replica scaling from frozen requests and every recorded response."""
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path

from benchmark import summarize, validate
from prepare import digest

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/replicas-retry-20261120"
FAMILY = ROOT / "experiments/family-20261120"


def load(path):
    return json.loads(path.read_text())


def choice(row):
    return row.get("response", {}).get("answers", {}).get("action", {}).get("choice")


def verify(family_reports):
    manifest = load(EXP / "manifest.json")
    original_manifest = load(ROOT / "experiments/replicas-20261120/manifest.json")
    assert manifest["selection"] == original_manifest["selection"]
    assert manifest["selection_rule_originally_frozen_utc"] == original_manifest["frozen_utc"]
    original_results = ROOT / "experiments/replicas-20261120/results"
    original_status = load(original_results / "suite-status.json")
    assert original_status["status"] == "failed" and not original_status["configs"]
    assert not list(original_results.rglob("requests.jsonl"))
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    status = load(EXP / "results/suite-status.json")
    assert status["status"] == "complete" and len(status["configs"]) == 2
    assert all(c["status"] == "complete" for c in status["configs"])
    selection = load(EXP / "results/selection.json")
    eligible = [r for r in family_reports if r["precision"] == "fp8"
                and r["new_errors_vs_31b"] == 0 and r["quality"]["valid"] == 104]
    selected = min(eligible, key=lambda r: r["quality"]["p50_ms"])
    assert selection["selected"]["config"]["id"] == selected["id"]
    assert selection["selected"]["p50_ms"] == selected["quality"]["p50_ms"]
    assert manifest["frozen_utc"] < selection["selected_utc"]
    fixture = {r["id"]: r for r in load(FAMILY / "requests.json")}
    reports, errors, records, observed = [], [], [], {}
    for ranks in manifest["replicas"]:
        directory = EXP / "results" / f"replicas-{ranks}"
        command = load(directory / "server-command.json")
        assert command[command.index("--nproc_per_node") + 1] == str(ranks)
        assert command[command.index("--max-length") + 1] == str(manifest["max_length"])
        assert command[command.index("--checkpoint") + 1].endswith("karpo-" + selected["model"].lower())
        assert command[command.index("--max-length") + 2:] == selected["flags"]
        health = load(directory / "health.json")
        assert health["ranks"] == ranks and health["path"] == "fp8-graphs"
        rows = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
        expected = {"warmup-excluded": 8, "quality-existing": 80, "quality-boundary": 24,
                    **{f"concurrency-{n}": 32 for n in manifest["concurrency"]}}
        assert collections.Counter(r["stage"] for r in rows) == expected
        assert sorted(r["sequence"] for r in rows) == list(range(1, len(rows) + 1))
        assert load(directory / "complete.json")["observations"] == len(rows) == 272
        summary = load(directory / "summary.json")
        for row in rows:
            item = fixture[row["request_id"]]
            for key in ["case_id", "group", "order", "description", "candidate_count", "expected"]:
                assert row[key] == item[key]
            assert row["request_sha256"] == item["request_sha256"] == digest(item["request"])
            assert len(item["request"]["questions"]["action"]["criteria"]) == row["candidate_count"] == 200
            if row["valid"]:
                assert validate(row["response"], item) == row["correct"]
                assert row["response"]["model"] == f"replicas-{ranks}"
                assert 0 < row["response"]["usage"]["input_tokens"] <= manifest["max_length"]
            else:
                assert not row["correct"]
            if row["stage"].startswith("quality-") and not row["correct"]:
                errors.append({"replicas": ranks, "state": item["request"]["state"], **row})
        for stage in expected:
            assert summarize([r for r in rows if r["stage"] == stage],
                             summary[stage]["elapsed_seconds"]) == summary[stage]
        quality = [r for r in rows if r["stage"].startswith("quality-")]
        assert len(quality) == 104 and {r["request_id"] for r in quality} == set(fixture)
        pooled = summarize(quality, sum(summary[s]["elapsed_seconds"]
                                       for s in ["quality-existing", "quality-boundary"]))
        reports.append({"replicas": ranks, "observations": len(rows), "summary": summary,
                        "quality": pooled, "health": health, "config": selected["id"]})
        observed[ranks] = {r["request_id"]: r for r in quality}
        records.append({"replicas": ranks, "observations": len(rows),
                        "raw_sha256": hashlib.sha256((directory / "requests.jsonl").read_bytes()).hexdigest()})
    comparison = {
        "same_choices": sum(a["valid"] and observed[2][key]["valid"]
                            and choice(a) == choice(observed[2][key]) for key, a in observed[1].items()),
        "new_errors": sum(a["correct"] and not observed[2][key]["correct"] for key, a in observed[1].items()),
        "fixed_errors": sum(not a["correct"] and observed[2][key]["correct"] for key, a in observed[1].items()),
    }
    assert sum(r["observations"] for r in reports) == manifest["expected_calls"] == 544
    postflight = load(EXP / "results/postflight.json")
    assert postflight["suite_status"] == "complete" and not postflight["gpu_compute_processes"]
    record = {"checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "all_complete": True, "total_calls": 544, "checks": records, "comparison": comparison}
    (EXP / "results/verification.json").write_text(json.dumps(record, indent=2) + "\n")
    (EXP / "results/quality-errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n")
    return {"configs": reports, "selected": selected["id"], "verification": record,
            "comparison": comparison, "manifest": manifest}
