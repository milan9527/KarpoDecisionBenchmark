"""Measured batching, semantic parity, concurrency and offered-load experiments."""
import argparse
import asyncio
from collections import Counter
import datetime as dt
import json
import math
from pathlib import Path
import statistics
import time

import httpx
from benchmark import percentile, validate

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/batch100-20261120"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    tmp.replace(path)


def summary(rows, elapsed):
    valid = [r for r in rows if r["valid"]]
    latency = [r["latency_ms"] for r in valid]
    all_latency = [r["latency_ms"] for r in rows]
    batches = Counter(str(r.get("response", {}).get("metrics", {}).get("batch_size")) for r in valid)
    tokens = [r["response"]["usage"]["input_tokens"] for r in valid]
    return {
        "attempts": len(rows), "valid": len(valid), "failures": len(rows) - len(valid),
        "correct": sum(r["correct"] for r in rows),
        "accuracy_all_attempts": sum(r["correct"] for r in rows) / len(rows) if rows else None,
        "valid_within_500ms": sum(r["valid"] and r["latency_ms"] <= 500 for r in rows),
        "correct_within_500ms": sum(r["correct"] and r["latency_ms"] <= 500 for r in rows),
        "elapsed_seconds": elapsed, "valid_rps": len(valid) / elapsed if elapsed else None,
        "p50_ms": percentile(latency, .5), "p95_ms": percentile(latency, .95),
        "p99_ms": percentile(latency, .99), "max_ms": max(latency, default=None),
        "all_attempts_p95_ms": percentile(all_latency, .95),
        "mean_ms": statistics.mean(latency) if latency else None,
        "tokens_min": min(tokens, default=None), "tokens_max": max(tokens, default=None),
        "response_batch_histogram": dict(batches),
        "http_statuses": dict(Counter(str(r.get("http_status")) for r in rows)),
        "max_client_send_lag_ms": max((r["send_lag_ms"] for r in rows), default=0),
        "max_gpu_allocated_gib": max((r["response"]["metrics"]["peak_allocated_gib"] for r in valid), default=None),
        "latency_scope": "Same host: scheduled arrival (open/burst) or invocation (closed), to complete HTTP response; includes queue/encode/model, excludes production network and feature retrieval.",
    }


class Runner:
    def __init__(self, client, directory):
        self.client, self.directory = client, directory
        directory.mkdir(parents=True, exist_ok=False)
        self.raw = (directory / "requests.jsonl").open("x")
        self.rows = []
        self.counter = 0

    def record(self, row):
        self.raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.raw.flush()
        self.rows.append(row)

    async def call(self, item, scheduled=None):
        start = time.perf_counter()
        scheduled = start if scheduled is None else scheduled
        self.counter += 1
        row = {"sequence": self.counter, "request_id": item["id"], "case_id": item["case_id"],
               "order": item["order"], "scenario": item["scenario"], "expected": item["expected"],
               "request_sha256": item["request_sha256"], "valid": False, "correct": False,
               "start_offset_s": scheduled - self.begin, "send_lag_ms": max(0, start - scheduled) * 1000}
        try:
            response = await self.client.post("/v1/systemone", json=item["request"])
            row["http_status"] = response.status_code
            row["response"] = response.json()
            response.raise_for_status()
            row["correct"] = validate(row["response"], item)
            row["valid"] = True
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        row["latency_ms"] = (time.perf_counter() - scheduled) * 1000
        self.record(row)

    async def closed(self, items, concurrency, count=None, seconds=None):
        pending = iter(range(count)) if count is not None else None
        index = 0
        async def worker():
            nonlocal index
            while True:
                if seconds is not None and time.perf_counter() - self.begin >= seconds:
                    return
                if pending is not None:
                    try:
                        i = next(pending)
                    except StopIteration:
                        return
                else:
                    i, index = index, index + 1
                await self.call(items[i % len(items)])
        await asyncio.gather(*(worker() for _ in range(concurrency)))

    async def offered(self, items, qps, seconds, cap=256):
        active = set()
        for i in range(int(qps * seconds)):
            scheduled = self.begin + i / qps
            await asyncio.sleep(max(0, scheduled - time.perf_counter()))
            if len(active) >= cap:
                item = items[i % len(items)]
                self.record({"sequence": None, "request_id": item["id"], "scenario": item["scenario"],
                             "valid": False, "correct": False, "latency_ms": (time.perf_counter() - scheduled) * 1000,
                             "send_lag_ms": (time.perf_counter() - scheduled) * 1000,
                             "error": f"client_inflight_limit_{cap}", "start_offset_s": i / qps})
            else:
                task = asyncio.create_task(self.call(items[i % len(items)], scheduled))
                active.add(task)
                task.add_done_callback(active.discard)
        if active:
            await asyncio.gather(*active)
        await asyncio.sleep(max(0, self.begin + seconds - time.perf_counter()))

    async def run(self, items, mode, **kwargs):
        self.begin = time.perf_counter()
        try:
            if mode == "burst":
                await asyncio.gather(*(self.call(items[i % len(items)], self.begin) for i in range(kwargs["count"])))
            elif mode == "open":
                await self.offered(items, **kwargs)
            else:
                await self.closed(items, **kwargs)
            result = summary(self.rows, time.perf_counter() - self.begin)
            result.update(mode=mode, parameters=kwargs)
            if kwargs.get("seconds"):
                # Include a middle measurement interval as well as total/drain statistics.
                seconds = kwargs["seconds"]
                middle = [r for r in self.rows if min(10, seconds / 4) <= r["start_offset_s"] < seconds]
                result["after_ramp"] = summary(
                    middle, time.perf_counter() - self.begin - min(10, seconds / 4)) if middle else None
                result["after_ramp_scope"] = "Requests arriving after ramp; throughput denominator includes final drain."
            write(self.directory / "summary.json", result)
            write(self.directory / "complete.json", {"status": "complete", "observations": len(self.rows)})
            print(str(self.directory.relative_to(EXP)), json.dumps({k: result[k] for k in
                  ["attempts", "valid", "correct", "valid_rps", "p95_ms"]}), flush=True)
            return result
        finally:
            self.raw.close()


async def measure(client, directory, items, mode="closed", **kwargs):
    if (directory / "complete.json").exists():
        return json.loads((directory / "summary.json").read_text())
    return await Runner(client, directory).run(items, mode, **kwargs)


async def configure(client, replicas=1, batch=1, wait=0):
    response = await client.post("/configure", json={"replicas": replicas, "max_batch": batch, "wait_ms": wait})
    response.raise_for_status()


def items_for(scenario):
    items = json.loads((EXP / f"{scenario}-requests.json").read_text())
    quality = [x for x in items if x["group"] == "quality"]
    tuning = [x for x in items if x["group"] == "tune" and x["order"] == "original"]
    if not tuning:
        tuning = [x for x in items if x["order"] == "original"]
    return quality, tuning


async def explicit_scan(client, directory, scenario, items, baseline):
    directory.mkdir(parents=True, exist_ok=False)
    rows, scans = [], []
    # Evenly spaced cases, spanning actions and candidate orders.
    chosen = [items[(i * len(items)) // 32] for i in range(32)]
    with (directory / "batches.jsonl").open("x") as raw:
        for batch in [1, 2, 4, 8, 16]:
            local = []
            for offset in range(0, len(chosen), batch):
                group = chosen[offset:offset + batch]
                start = time.perf_counter()
                response = await client.post("/batch", json={"requests": [r["request"] for r in group]})
                elapsed = time.perf_counter() - start
                body = response.json()
                record = {"batch": batch, "request_ids": [r["id"] for r in group], "http_status": response.status_code,
                          "elapsed_seconds": elapsed, "response": body}
                raw.write(json.dumps(record, ensure_ascii=False) + "\n")
                raw.flush()
                response.raise_for_status()
                assert len(body["responses"]) == len(group)
                parity = []
                for item, answer in zip(group, body["responses"], strict=True):
                    correct = validate(answer, item)
                    reference = baseline[item["id"]]["response"]["answers"]["action"]
                    action = answer["answers"]["action"]
                    parity.append({"request_id": item["id"], "correct": correct,
                                   "same_choice": action["choice"] == reference["choice"],
                                   "max_probability_delta": max(abs(p - reference["probabilities"][k])
                                                                for k, p in action["probabilities"].items())})
                local.append({"seconds": elapsed, "count": len(group), "metrics": body["metrics"], "parity": parity})
            parity = [p for r in local for p in r["parity"]]
            scans.append({"batch": batch, "requests": len(parity),
                          "valid_rps": len(parity) / sum(r["seconds"] for r in local),
                          "batch_p95_ms": percentile([r["seconds"] * 1000 for r in local], .95),
                          "same_choices": sum(r["same_choice"] for r in parity),
                          "correct": sum(r["correct"] for r in parity),
                          "max_probability_delta": max(r["max_probability_delta"] for r in parity),
                          "max_packs": max(r["metrics"]["packs"] for r in local),
                          "graph_replays": sum(r["metrics"]["graph_replays"] for r in local),
                          "peak_allocated_gib": max(r["metrics"]["peak_allocated_gib"] for r in local)})
            rows.extend(local)
            write(directory / "summary.json", scans)
            print(scenario, "explicit", scans[-1], flush=True)
    write(directory / "parity.json", rows)
    write(directory / "complete.json", {"status": "complete"})


def parity(reference_dir, actual_dir):
    baseline = {r["request_id"]: r for r in map(json.loads, (reference_dir / "requests.jsonl").read_text().splitlines())}
    rows = [json.loads(line) for line in (actual_dir / "requests.jsonl").read_text().splitlines()]
    details = []
    for row in rows:
        if not row["valid"]:
            details.append({"request_id": row["request_id"], "invalid": True})
            continue
        base = baseline[row["request_id"]]
        a, b = base["response"]["answers"]["action"], row["response"]["answers"]["action"]
        details.append({"request_id": row["request_id"], "same_choice": a["choice"] == b["choice"],
                        "reference_correct": base["correct"], "actual_correct": row["correct"],
                        "max_probability_delta": max(abs(p - b["probabilities"][k]) for k, p in a["probabilities"].items())})
    result = {"observations": len(rows), "same_choices": sum(r.get("same_choice", False) for r in details),
              "new_errors": sum(r.get("reference_correct", False) and not r.get("actual_correct", False) for r in details),
              "max_probability_delta": max((r.get("max_probability_delta", 0) for r in details), default=None),
              "details": details}
    write(actual_dir / "parity-vs-reference.json", result)


async def main(phase, url):
    manifest = json.loads((EXP / "manifest.json").read_text())
    target = EXP / "results" / phase
    target.mkdir(exist_ok=True)
    async with httpx.AsyncClient(base_url=url, timeout=180, trust_env=False,
                                limits=httpx.Limits(max_connections=300, max_keepalive_connections=300)) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        write(target / "health.json", health.json())
        for scenario in ("risk", "sop"):
            quality, tuning = items_for(scenario)
            base = target / scenario
            await configure(client)
            await measure(client, base / "warmup", tuning[:8], concurrency=1, count=8)
            await measure(client, base / "quality-serial", quality, concurrency=1, count=len(quality))
            if phase == "bf16":
                continue
            parity(EXP / "results/bf16" / scenario / "quality-serial", base / "quality-serial")
            baseline = {r["request_id"]: r for r in map(json.loads,
                        (base / "quality-serial/requests.jsonl").read_text().splitlines())}
            if not (base / "explicit/complete.json").exists():
                await explicit_scan(client, base / "explicit", scenario, quality, baseline)
            candidates = []
            for batch in manifest["batch_sizes"]:
                for wait in ([0] if batch == 1 else manifest["wait_ms"]):
                    await configure(client, batch=batch, wait=wait)
                    name = f"tuning-b{batch}-w{wait}"
                    result = await measure(client, base / name, tuning, concurrency=32,
                                           count=manifest["scan_requests"])
                    candidates.append({"batch": batch, "wait_ms": wait, "summary": result})
            eligible = [c for c in candidates if c["summary"]["failures"] == 0]
            if not eligible:
                raise RuntimeError(f"No valid configuration for {scenario}")
            chosen = max(eligible, key=lambda c: (c["summary"]["valid_rps"], -c["summary"]["p95_ms"]))
            write(base / "selection.json", {"selected": chosen, "candidates": candidates})
            for replicas in manifest["replicas"]:
                await configure(client, replicas, chosen["batch"], chosen["wait_ms"])
                rep = base / f"replicas-{replicas}"
                await measure(client, rep / "quality", quality, concurrency=32, count=len(quality))
                parity(base / "quality-serial", rep / "quality")
                for concurrency in manifest["concurrency"]:
                    await measure(client, rep / f"concurrency-{concurrency}", tuning, concurrency=concurrency,
                                  count=max(manifest["scan_requests"], concurrency * 2))
                await measure(client, rep / "burst-100", tuning, mode="burst", count=100)
                await measure(client, rep / "sustained-100", tuning, concurrency=100,
                              seconds=manifest["sustained_seconds"])
                for qps in manifest["open_loop_qps"]:
                    await measure(client, rep / f"arrival-{qps}", tuning, mode="open", qps=qps,
                                  seconds=manifest["open_loop_seconds"], cap=manifest["client_max_inflight"])
    write(target / "complete.json", {"status": "complete", "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat()})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--phase", choices=["bf16", "fp8"], required=True)
    p.add_argument("--url", default="http://127.0.0.1:18450")
    args = p.parse_args()
    asyncio.run(main(args.phase, args.url))
