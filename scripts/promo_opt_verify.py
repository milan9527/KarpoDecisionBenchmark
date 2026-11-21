"""Independent replay of recorded response/encoding/latency checks for every run."""
from collections import Counter
import copy
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

from benchmark import percentile, validate
from promo_risk import digest
from promo_risk_evaluate import evaluate_responses
from vllm_rps_benchmark import summarize

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-opt-20261121"


def read(path):
    return json.loads(path.read_text())


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def verify_run(directory):
    status = read(directory / "status.json")
    assert status["status"] in ("complete", "cancelled_by_user"), directory
    params = read(directory / "parameters.json")
    calibrated = status.get("decision_policy") == "review-threshold-v1"
    calibration = read(EXP / "review-calibration-v1.json") if calibrated else None
    data = EXP / "inputs" / params["dataset"]
    for name, expected in read(data / "manifest.json")["files"].items():
        assert hashlib.sha256((data / name).read_bytes()).hexdigest() == expected
    fixtures = read(data / "risk-requests.json")
    by_id = {row["id"]: row for row in fixtures}
    assert len(by_id) == len(fixtures)
    assert all(digest(r["request"]) == r["request_sha256"] for r in fixtures)
    encodings = {r["request_id"]: r for r in read(EXP / "results/encodings" / (params["dataset"] + ".json"))}
    assert set(encodings) == set(by_id)
    stages = []
    raw_quality = None
    for step in status["stages"]:
        name, recorded = step["name"], step["result"]
        raw = [json.loads(line) for line in (directory / name / "requests.jsonl").read_text().splitlines()]
        assert sorted(r["sequence"] for r in raw) == list(range(len(raw)))
        for row in raw:
            fixture = by_id[row["request_id"]]
            assert row["request_sha256"] == fixture["request_sha256"]
            assert row["expected"] == fixture["expected"]
            assert abs(row["latency_ms"] - 1000 * (row["end_offset_s"] - row["scheduled_offset_s"])) < .0001
            assert abs(row["http_latency_ms"] - 1000 * (row["end_offset_s"] - row["send_offset_s"])) < .0001
            valid = correct = False
            try:
                assert row.get("http_status") == 200
                correct = validate(row["response"], fixture)
                ref = encodings[row["request_id"]]
                metrics = row["response"]["metrics"]
                assert metrics["tokens_sha256"] == ref["tokens_sha256"]
                assert metrics["marker"] == ref["marker"]
                assert row["response"]["usage"]["input_tokens"] == ref["length"]
                assert metrics["replica_id"] == metrics["proxy_replica"]
                assert metrics["replica_id"] in range(4)
                assert metrics["backend"] == "vllm" and metrics["max_num_seqs"] == params["batch"]
                if calibrated:
                    assert row["raw_correct"] == correct
                    answer = row["response"]["answers"]["action"]
                    probabilities = answer["probabilities"]
                    denominator = probabilities["review"] + probabilities["allow"]
                    score = probabilities["review"] / denominator if denominator else 0.0
                    threshold = calibration["selected"]["threshold"]
                    chosen = "review" if answer["choice"] == "allow" and score >= threshold else answer["choice"]
                    decision = row["response"]["decision"]
                    assert decision == {
                        "action": chosen, "raw_action": answer["choice"], "review_pair_score": score,
                        "threshold": threshold, "escalated": chosen != answer["choice"],
                        "policy": "review-threshold-v1",
                        "calibration_sha256": hashlib.sha256((EXP / "review-calibration-v1.json").read_bytes()).hexdigest(),
                    }
                    correct = chosen == fixture["expected"]["action"]
                valid = True
            except (AssertionError, KeyError, ValueError, TypeError):
                correct = False
            assert row["valid"] == valid and row["correct"] == correct, row["request_id"]
        if name in ("warmup", "tune", "quality"):
            group = "quality" if name == "quality" else "tune"
            expected = [r for r in fixtures if r["group"] == group]
            if name == "warmup":
                expected = expected[:80 if params["protocol"] == "formal" else 16]
            assert sorted(r["request_id"] for r in raw) == sorted(r["id"] for r in expected)
            result = {"attempts": len(raw), "valid": sum(r["valid"] for r in raw),
                      "correct": sum(r["correct"] for r in raw)}
            result.update(accuracy=result["correct"] / result["attempts"],
                          failures=result["attempts"] - result["valid"])
            assert result == recorded
            if name == "quality":
                raw_quality = raw
        else:
            rate, seconds = recorded["target_rps"], recorded["offered_seconds"]
            assert len(raw) == round(rate * seconds)
            tune = [r for r in fixtures if r["group"] == "tune"]
            for row in raw:
                assert row["request_id"] == tune[row["sequence"] % len(tune)]["id"]
                assert abs(row["scheduled_offset_s"] - row["sequence"] / rate) < .0001
            result = summarize(raw, rate, seconds, recorded["elapsed_with_drain_s"])
            assert result == recorded, (directory.name, name)
        stages.append({"name": name, **result})
        if calibrated:
            stages[-1]["raw_correct"] = sum(r.get("raw_correct", False) for r in raw)
            stages[-1]["calibration_p50_ms"] = percentile([
                r["response"]["metrics"]["calibration_ms"] for r in raw if r["valid"]], .5)
    health = read(directory / "health.json")
    assert len(health["backends"]) == 4
    for replica, backend in enumerate(health["backends"]):
        assert backend["replica_id"] == replica and backend["max_num_seqs"] == params["batch"]
        assert backend["graph_policy"] == params["graph"]
        settings = backend["settings"]
        assert settings["quantization"] == "fp8_per_channel"
        assert settings["enable_prefix_caching"] is False and settings["enable_chunked_prefill"] is False
    output = {"name": directory.name, "dataset": params["dataset"], "protocol": params["protocol"],
              "status": status["status"],
              "decision_policy": "review-threshold-v1" if calibrated else "raw-karpo",
              "batch": params["batch"], "graph": params["graph"], "stages": stages,
              "verified_requests": sum(s["attempts"] for s in stages)}
    if raw_quality is not None and "behavior" in fixtures[0]:
        scored = raw_quality
        if calibrated:
            output["raw_quality"] = evaluate_responses(fixtures, raw_quality)
            scored = copy.deepcopy(raw_quality)
            for row in scored:
                if row["valid"]:
                    row["response"]["answers"]["action"]["choice"] = row["response"]["decision"]["action"]
        quality = evaluate_responses(fixtures, scored)
        if calibrated:
            quality["scope"] = "Actual HTTP endpoint with frozen review-score threshold; raw Karpo answers separately retained"
        confusion = Counter((r["expected"]["action"], r["response"]["answers"]["action"]["choice"])
                            for r in scored if r["valid"])
        quality["confusion"] = [{"expected": a, "actual": b, "count": c} for (a, b), c in sorted(confusion.items())]
        output["quality"] = quality
    if params["protocol"] == "formal" and status["status"] == "complete":
        q = next(s for s in stages if s["name"] == "quality")
        formal = [s for s in stages if s["name"] in ("confirm", "repeat")]
        output["quality_passed"] = q["accuracy"] >= .9 and q["failures"] == 0
        output["performance_passed"] = len(formal) == 2 and all(s["passes_performance_acceptance"] for s in formal)
        output["pressure_quality_passed"] = len(formal) == 2 and all(s["correct"] / s["attempts"] >= .9 for s in formal)
        output["accepted"] = all(output[k] for k in ("quality_passed", "performance_passed", "pressure_quality_passed"))
        assert output["accepted"] == status["accepted"]
    return output


def main():
    for manifest in ("manifest-v1.json", "review-manifest-v1.json", "review-controller-v1.json"):
        for name, expected in read(EXP / manifest)["files"].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    if (EXP / "calibration-runtime-v1.json").exists():
        for name, expected in read(EXP / "calibration-runtime-v1.json")["files"].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
        from promo_opt_calibration import verify_fit
        verify_fit()
        assert dt.datetime.fromisoformat(read(EXP / "review-calibration-v1.json")["frozen_utc"]) < dt.datetime.fromisoformat(
            read(EXP / "results/runs/review-fresh-baseline/status.json")["started_utc"])
    runs = [verify_run(p.parent) for p in sorted((EXP / "results/runs").glob("*/status.json"))]
    by_name = {run["name"]: run for run in runs}
    selection = read(EXP / "results/review-selection.json")
    for candidate in selection["candidates"]:
        run = by_name["review-screen-" + candidate["variant"]]
        tune = next(s for s in run["stages"] if s["name"] == "tune")
        assert candidate["tune"] == {k: tune[k] for k in candidate["tune"]}
        rows = [json.loads(line) for line in (
            EXP / "results/runs" / run["name"] / "tune/requests.jsonl").read_text().splitlines()]
        assert candidate["review_attempts"] == sum(r["expected"]["action"] == "review" for r in rows)
        assert candidate["review_correct"] == sum(
            r["expected"]["action"] == "review" and r["correct"] for r in rows)
        fixtures = read(EXP / "inputs" / run["dataset"] / "risk-requests.json")
        mean_bytes = sum(len(json.dumps(r["request"], ensure_ascii=False).encode())
                         for r in fixtures if r["group"] == "tune") / tune["attempts"]
        assert mean_bytes == candidate["mean_json_bytes"]
    best = sorted([c for c in selection["candidates"] if c["tune"]["failures"] == 0],
                  key=lambda c: (-c["tune"]["accuracy"], -c["review_correct"],
                                 c["mean_json_bytes"], c["variant"]))[0]
    assert best == selection["selected"]
    assert dt.datetime.fromisoformat(selection["frozen_utc"]) < dt.datetime.fromisoformat(
        read(EXP / "results/runs/review-fresh-baseline/status.json")["started_utc"])
    engines = sorted((EXP / "results/nodes").glob("*/replica-*/engine-config.json"))
    assert engines, "Missing real GPU engine proofs"
    fleets, fleet_settings = {}, {}
    expected_head = "e1682b846b9a86eb3daf7faf93caf7368fb46a882e99f6ef5bd54c2c8a517a3f"
    for path in engines:
        info = read(path)
        node = read(path.parent.parent / "parameters.json")
        fleet = node["name"].rsplit("-", 1)[0]
        replicas = fleets.setdefault(fleet, set())
        replica = int(path.parent.name.split("-")[-1])
        assert replica not in replicas, (fleet, replica)
        replicas.add(replica)
        fleet_settings.setdefault(fleet, []).append(info["settings"])
        assert info["settings"]["quantization"] == "fp8_per_channel"
        assert info["full_attention_is_causal"] is False
        for worker in info["workers"]:
            assert worker["readout_dtype"] == "torch.float32" and worker["readout_sha256"] == expected_head
            assert len(worker["attention"]) == 8
            assert all("ENCODER_ONLY" in row["type"].upper() for row in worker["attention"])
    # User-cancelled startup may leave a partial fleet. Every measured run must
    # still match four independently inspected, identically configured replicas.
    for run in runs:
        info = read(EXP / "results/runs" / run["name"] / "health.json")
        settings = info["backends"][0]["settings"]
        assert any(ids == set(range(4)) and all(s == settings for s in fleet_settings[fleet])
                   for fleet, ids in fleets.items()), run["name"]
    environments = [read(p) for p in sorted((EXP / "results/nodes").glob("*/environment.json"))]
    for key in ("torch", "transformers", "vllm", "cuda", "karpo_source_sha256",
                "model_metadata_sha256", "model_weights_bytes", "adapter_sha256"):
        assert all(e[key] == environments[0][key] for e in environments), key
    expected_adapter = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (ROOT / "serving/vllm_karpo").glob("karpo_vllm*.py")}
    assert environments[0]["adapter_sha256"] == expected_adapter
    gpu = []
    for run in runs:
        if run["protocol"] != "formal" or run["status"] != "complete":
            continue
        for stage in ("confirm", "repeat"):
            timing = read(EXP / "results/runs" / run["name"] / stage / "timing.json")
            start = dt.datetime.fromisoformat(timing["begin_utc"]) + dt.timedelta(seconds=30)
            end = dt.datetime.fromisoformat(timing["offered_end_utc"])
            values = {i: [] for i in range(4)}
            for path in sorted((EXP / "results/nodes").glob("*/gpu.csv")):
                params = read(path.parent / "parameters.json")
                with path.open() as stream:
                    for row in csv.DictReader(stream, skipinitialspace=True):
                        stamp = dt.datetime.strptime(row["timestamp"], "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
                        if start <= stamp < end:
                            values[params["replica_ids"][int(row["index"])]].append(
                                float(row["utilization.gpu [%]"].split()[0]))
            for replica, samples in values.items():
                assert samples, (run["name"], stage, replica)
                gpu.append({"run": run["name"], "stage": stage, "replica": replica,
                            "mean_percent": sum(samples) / len(samples), "samples": len(samples)})
    result = {"runs": runs, "verified_requests": sum(r["verified_requests"] for r in runs),
              "engine_proofs": len(engines), "readout_sha256": expected_head, "gpu_utilization": gpu,
              "startup_only_partial_fleets": {fleet: sorted(ids) for fleet, ids in fleets.items()
                                             if ids != set(range(4))},
              "verified_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    dump(EXP / "results/verification.json", result)
    final_name = "review-calibrated-confirmed" if "review-calibrated-confirmed" in by_name else "review-confirmed"
    if final_name in by_name and "review-fresh-baseline" in by_name:
        final_run = by_name[final_name]
        fixtures = {r["id"]: r for r in read(
            EXP / "inputs" / final_run["dataset"] / "risk-requests.json")}
        def quality_rows(name):
            return [json.loads(line) for line in (
                EXP / "results/runs" / name / "quality/requests.jsonl").read_text().splitlines()]
        previous = {row["request_id"]: row for row in quality_rows("review-fresh-baseline")}
        current = quality_rows(final_name)
        example = next((r for r in current if r["expected"]["action"] == "review" and r["correct"]
                        and not previous[r["request_id"]]["correct"]), current[0])
        fixture = fixtures[example["request_id"]]
        dump(EXP / "results/decision-example.json", {
            "request_id": example["request_id"], "request": fixture["request"],
            "request_sha256": fixture["request_sha256"], "expected": fixture["expected"],
            "actual_response": example.get("response"), "latency_ms": example["latency_ms"],
            "baseline_response": previous[example["request_id"]].get("response"),
            "baseline_request_sha256": previous[example["request_id"]]["request_sha256"],
            "source": "Final quality pass; baseline response from the same fresh heldout case",
        })
    print(json.dumps({k: result[k] for k in ("verified_requests", "engine_proofs", "verified_utc")}))


if __name__ == "__main__":
    main()
