"""Identical original-input HTTP workload for the native and vLLM services."""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics

import httpx

import batch100_benchmark as bench
from benchmark import percentile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-single-20261120"
bench.EXP = EXP


def inflight(rows, start=10.0, end=60.0):
    events = []
    for row in rows:
        begin = row["start_offset_s"] + row["send_lag_ms"] / 1000
        finish = row["start_offset_s"] + row["latency_ms"] / 1000
        events.extend([(begin, 1), (finish, -1)])
    active = peak = 0
    area = 0.0
    previous = 0.0
    for point, delta in sorted(events):
        area += max(0, min(point, end) - max(previous, start)) * active
        active += delta
        peak = max(peak, active)
        previous = point
    return {"peak": peak, "mean_10_to_60s": area / (end - start)}


def summary(rows, elapsed):
    valid = [r for r in rows if r["valid"]]
    latencies = [r["latency_ms"] for r in valid]
    tokens = [r["response"]["usage"]["input_tokens"] for r in valid]
    result = {
        "attempts": len(rows), "valid": len(valid), "failures": len(rows) - len(valid),
        "correct": sum(r["correct"] for r in rows),
        "accuracy_all_attempts": sum(r["correct"] for r in rows) / len(rows) if rows else None,
        "valid_within_500ms": sum(r["valid"] and r["latency_ms"] <= 500 for r in rows),
        "elapsed_seconds": elapsed, "valid_rps": len(valid) / elapsed if elapsed else None,
        "p50_ms": percentile(latencies, .5), "p95_ms": percentile(latencies, .95),
        "p99_ms": percentile(latencies, .99), "mean_ms": statistics.mean(latencies) if latencies else None,
        "tokens_min": min(tokens, default=None), "tokens_max": max(tokens, default=None),
        "http_statuses": dict(Counter(str(r.get("http_status")) for r in rows)),
        "max_client_send_lag_ms": max((r["send_lag_ms"] for r in rows), default=0),
        "latency_scope": "Same-host loopback: invocation to complete HTTP response, including encoding and queue; excludes production ingress and feature retrieval.",
    }
    if rows and max(r["start_offset_s"] for r in rows) >= 55:
        result["inflight"] = inflight(rows)
    return result


bench.summary = summary


async def main(args):
    target = EXP / "results/measurements" / args.name
    manifest = json.loads((EXP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    bench.write(target / "source-manifest.json", manifest)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18450", timeout=180, trust_env=False,
        limits=httpx.Limits(max_connections=150, max_keepalive_connections=150)
    ) as client:
        response = await client.get("/healthz")
        response.raise_for_status()
        bench.write(target / "health.json", response.json())
        if args.backend == "native":
            await bench.configure(client, 1, 1, 0)
        scenarios = [args.scenario] if args.scenario else ["risk", "sop"]
        encodings = {
            r["request_id"]: r for r in json.loads((EXP / "results/encoding/native.json").read_text())
        }
        for scenario in scenarios:
            quality, tuning = bench.items_for(scenario)
            base = target / scenario
            await bench.measure(client, base / "warmup", tuning, concurrency=1, count=8)
            if not args.confirm:
                result = await bench.measure(
                    client, base / "quality", quality,
                    concurrency=1 if args.backend == "native" else 100, count=len(quality)
                )
                if result["failures"]:
                    raise RuntimeError(f"{scenario} quality requests failed")
                reference = (
                    ROOT / "experiments/batch100-20261120/results/fp8" / scenario / "quality-serial"
                    if args.name == "native-initial"
                    else EXP / "results/measurements/native-initial" / scenario / "quality"
                )
                bench.parity(
                    ROOT / "experiments/batch100-20261120/results/bf16" / scenario / "quality-serial",
                    base / "quality"
                )
                (base / "quality/parity-vs-reference.json").rename(base / "quality/parity-vs-historical-bf16.json")
                bench.parity(reference, base / "quality")
                if args.backend == "vllm":
                    rows = [json.loads(x) for x in (base / "quality/requests.jsonl").read_text().splitlines()]
                    for row in rows:
                        expected = encodings[row["request_id"]]
                        actual = row["response"]
                        assert actual["metrics"]["tokens_sha256"] == expected["tokens_sha256"]
                        assert actual["metrics"]["marker"] == expected["marker"]
                        assert actual["usage"]["input_tokens"] == expected["length"]
                    bench.write(base / "quality/token-parity.json", {"identical": len(rows), "total": len(rows)})
                await bench.measure(
                    client, base / "serial", tuning, concurrency=1, count=max(60, len(tuning))
                )
            result = await bench.measure(
                client, base / "sustained-100", tuning, concurrency=100, seconds=60
            )
            if result["failures"] or result["inflight"]["peak"] != 100 or result["inflight"]["mean_10_to_60s"] < 99:
                raise RuntimeError(f"{scenario}: invalid sustained C100 run")
        bench.write(target / "complete.json", {"status": "complete"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["native", "vllm"], required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--scenario", choices=["risk", "sop"])
    parser.add_argument("--confirm", action="store_true")
    asyncio.run(main(parser.parse_args()))
