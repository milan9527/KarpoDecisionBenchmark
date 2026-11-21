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
EXP = ROOT / "experiments/promo-rtx6-20261121"


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
                assert metrics["replica_id"] in range(6)
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
    assert len(health["backends"]) == 6
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
    manifest = read(EXP / "manifest-v1.json")
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    previous = ROOT / "experiments/promo-opt-20261121"
    assert (EXP / "inputs/review-baseline/risk-requests.json").read_bytes() == (
        previous / "inputs/review-baseline/risk-requests.json").read_bytes()
    assert (EXP / "review-calibration-v1.json").read_bytes() == (
        previous / "review-calibration-v1.json").read_bytes()
    assert read(EXP / "results/encodings/review-baseline.json") == read(
        EXP / "inputs/review-baseline/expected-encoding.json")
    assert read(EXP / "results/controller.json")["status"] == "complete"
    run = verify_run(EXP / "results/runs/rtx6-calibrated-v1")
    assert run["verified_requests"] == 27960 and run["batch"] == 4 and run["graph"] == "powers"
    for stage in run["stages"]:
        if stage["name"] in ("confirm", "repeat"):
            assert {int(i) for i, count in stage["replica_counts"].items() if count > 0} == set(range(6))
    fleet = read(EXP / "fleet-v1.json")
    engines = sorted((EXP / "results/nodes").glob("*/replica-*/engine-config.json"))
    assert len(engines) == 6
    replicas = set()
    expected_head = "e1682b846b9a86eb3daf7faf93caf7368fb46a882e99f6ef5bd54c2c8a517a3f"
    health = read(EXP / "results/runs/rtx6-calibrated-v1/health.json")
    for path in engines:
        replica = int(path.parent.name.split("-")[-1])
        assert replica not in replicas and replica in range(6)
        replicas.add(replica)
        info = read(path)
        assert info["settings"] == health["backends"][replica]["settings"]
        assert info["settings"]["quantization"] == "fp8_per_channel"
        assert info["full_attention_is_causal"] is False
        for worker in info["workers"]:
            assert worker["readout_dtype"] == "torch.float32" and worker["readout_sha256"] == expected_head
            assert len(worker["attention"]) == 8
            assert all("ENCODER_ONLY" in row["type"].upper() for row in worker["attention"])
    environments = []
    for host in fleet["hosts"]:
        directory = EXP / "results/nodes" / host["name"]
        assert read(directory / "parameters.json")["replica_ids"] == host["replica_ids"]
        environments.append(read(directory / "environment.json"))
        with (directory / "hardware.csv").open() as stream:
            hardware = list(csv.reader(stream, skipinitialspace=True))[1:]
        assert len(hardware) == len(host["replica_ids"])
        assert all("RTX PRO 6000 Blackwell Server Edition" in row[1] and
                   int(row[2].split()[0]) >= 95000 for row in hardware)
        if host["new"]:
            assert read(EXP / "results/setup" / (host["name"]+"-weights.json"))["weights"] == fleet["expected_weights"]
    for key in ("torch", "transformers", "vllm", "cuda", "karpo_source_sha256",
                "model_metadata_sha256", "model_weights_bytes", "adapter_sha256"):
        assert all(e[key] == environments[0][key] for e in environments), key
    assert environments[0]["adapter_sha256"] == {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "serving/vllm_karpo").glob("karpo_vllm*.py")}
    gpu = []
    for stage in ("confirm", "repeat"):
        timing = read(EXP / "results/runs" / run["name"] / stage / "timing.json")
        start = dt.datetime.fromisoformat(timing["begin_utc"]) + dt.timedelta(seconds=30)
        end = dt.datetime.fromisoformat(timing["offered_end_utc"])
        for host in fleet["hosts"]:
            values = {i: [] for i in host["replica_ids"]}
            with (EXP / "results/nodes" / host["name"] / "gpu.csv").open() as stream:
                for row in csv.DictReader(stream, skipinitialspace=True):
                    stamp = dt.datetime.strptime(row["timestamp"], "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
                    if start <= stamp < end:
                        values[host["replica_ids"][int(row["index"])]].append(float(row["utilization.gpu [%]"].split()[0]))
            for replica, samples in values.items():
                assert len(samples) >= 40, (stage, replica, len(samples))
                gpu.append({"run": run["name"], "stage": stage, "replica": replica,
                            "region": host["region"], "samples": len(samples),
                            "mean_percent": sum(samples)/len(samples)})
    comparison = next(r for r in read(previous / "results/verification.json")["runs"]
                      if r["name"] == "review-calibrated-confirmed")
    result = {"runs": [run], "four_gpu_comparison": comparison, "verified_requests": 27960,
              "engine_proofs": 6, "readout_sha256": expected_head, "gpu_utilization": gpu,
              "raw_input_and_calibration_identical_to_four_gpu": True,
              "network_difference": "Six-GPU test adds two Virginia replicas over SSH tunnels",
              "verified_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    dump(EXP / "results/verification.json", result)
    fixtures = {r["id"]: r for r in read(EXP / "inputs/review-baseline/risk-requests.json")}
    rows = [json.loads(line) for line in (
        EXP / "results/runs" / run["name"] / "quality/requests.jsonl").read_text().splitlines()]
    example = next(r for r in rows if r["valid"] and r["correct"] and r["response"]["decision"]["escalated"])
    dump(EXP / "results/decision-example.json", {
        "request_id": example["request_id"], "request": fixtures[example["request_id"]]["request"],
        "request_sha256": example["request_sha256"], "expected": example["expected"],
        "actual_response": example["response"], "latency_ms": example["latency_ms"],
        "source": "Six-GPU actual HTTP quality stage, unchanged frozen review threshold"})
    print(json.dumps({"verified_requests": 27960, "engine_proofs": 6, "accepted": run["accepted"]}))


if __name__ == "__main__":
    main()
