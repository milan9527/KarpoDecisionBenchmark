"""Fixed arrival times independent of responses; preserve every attempt and failure."""
import argparse
import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import statistics
import time

import httpx

from benchmark import percentile, validate

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def summarize(rows, rate, seconds, elapsed):
    valid = [r for r in rows if r["valid"]]
    last = [r for r in valid if r["scheduled_offset_s"] >= seconds * .75]
    steady_start = seconds * .25
    steady = [r for r in valid if steady_start <= r["end_offset_s"] < seconds]
    occupancy = [
        {"second": second, "pending": sum(r["send_offset_s"] <= second < r["end_offset_s"] for r in rows)}
        for second in range(1, math.floor(seconds) + 1)
    ]
    tail_occupancy = [r for r in occupancy if r["second"] >= steady_start]
    slope = None
    if len(tail_occupancy) > 1:
        x = [r["second"] for r in tail_occupancy]
        y = [r["pending"] for r in tail_occupancy]
        mx, my = statistics.mean(x), statistics.mean(y)
        slope = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sum((a - mx) ** 2 for a in x)
    result = {
        "target_rps": rate, "offered_seconds": seconds,
        "scheduled_requests": round(rate * seconds), "attempts": len(rows),
        "valid": len(valid), "failures": len(rows) - len(valid),
        "http_statuses": dict(Counter(str(r.get("http_status")) for r in rows)),
        "replica_counts": dict(Counter(str(r["response"]["metrics"].get("proxy_replica")) for r in valid)),
        "correct": sum(r["correct"] for r in valid),
        "elapsed_with_drain_s": elapsed,
        "valid_rps_with_drain": len(valid) / elapsed,
        "steady_valid_rps": len(steady) / (seconds - steady_start),
        "p50_ms": percentile([r["latency_ms"] for r in valid], .5),
        "p95_ms": percentile([r["latency_ms"] for r in valid], .95),
        "p99_ms": percentile([r["latency_ms"] for r in valid], .99),
        "http_p50_ms": percentile([r["http_latency_ms"] for r in valid], .5),
        "last_quarter_p50_ms": percentile([r["latency_ms"] for r in last], .5),
        "max_send_lag_ms": max((r["send_lag_ms"] for r in rows), default=0),
        "valid_within_500ms": sum(r["latency_ms"] <= 500 for r in valid),
        "backlog_samples": occupancy, "backlog_slope_per_second": slope,
        "latency_scope": "Scheduled arrival to complete HTTP body, conservatively including client send lateness. HTTP-only latency is also retained. Queue, encode, inference and measured network are included.",
    }
    result["passes_probe"] = bool(
        len(rows) == round(rate * seconds) and not result["failures"]
        and result["p50_ms"] <= 500 and result["last_quarter_p50_ms"] <= 500
        and result["steady_valid_rps"] >= .99 * rate and result["max_send_lag_ms"] <= 100
        and slope is not None and slope <= .1
    )
    result["passes_performance_acceptance"] = (
        rate == 100 and seconds >= 120 and result["passes_probe"]
    )
    return result


class Runner:
    def __init__(self, client, directory, encodings=None):
        self.client = client
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.encodings = encodings or {}
        self.rows = []
        self.begin = None
        self.raw = (directory / "requests.jsonl").open("x")

    async def call(self, item, sequence, scheduled):
        begin = time.perf_counter()
        row = {
            "sequence": sequence, "request_id": item["id"],
            "request_sha256": item["request_sha256"], "expected": item["expected"],
            "scheduled_offset_s": scheduled - self.begin, "send_offset_s": begin - self.begin,
            "send_lag_ms": max(0, begin - scheduled) * 1000, "valid": False, "correct": False,
        }
        end = None
        try:
            response = await self.client.post("/v1/systemone", json=item["request"])
            end = time.perf_counter()
            row["http_status"] = response.status_code
            row["response"] = response.json()
            response.raise_for_status()
            row["correct"] = validate(row["response"], item)
            if self.encodings:
                expected = self.encodings[item["id"]]
                metrics = row["response"]["metrics"]
                assert metrics["tokens_sha256"] == expected["tokens_sha256"]
                assert metrics["marker"] == expected["marker"]
                assert row["response"]["usage"]["input_tokens"] == expected["length"]
            row["valid"] = True
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {str(error)[:1500]}"
        end = end if end is not None else time.perf_counter()
        row.update(end_offset_s=end - self.begin, latency_ms=(end - scheduled) * 1000,
                   http_latency_ms=(end - begin) * 1000)
        self.rows.append(row)
        self.raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.raw.flush()
        return row

    async def fixed_rate(self, items, rate, seconds):
        self.begin = time.perf_counter() + .1
        tasks = []
        try:
            for sequence in range(round(rate * seconds)):
                scheduled = self.begin + sequence / rate
                await asyncio.sleep(max(0, scheduled - time.perf_counter()))
                # There is deliberately no response-dependent semaphore here.
                tasks.append(asyncio.create_task(self.call(items[sequence % len(items)], sequence, scheduled)))
            await asyncio.sleep(max(0, self.begin + seconds - time.perf_counter()))
            await asyncio.gather(*tasks)
            result = summarize(self.rows, rate, seconds, time.perf_counter() - self.begin)
            dump(self.directory / "summary.json", result)
            return result
        finally:
            self.raw.close()

    async def quality(self, items, concurrency):
        self.begin = time.perf_counter()
        work = iter(enumerate(items))

        async def worker():
            for sequence, item in work:
                await self.call(item, sequence, time.perf_counter())

        try:
            await asyncio.gather(*(worker() for _ in range(concurrency)))
            result = {"attempts": len(self.rows), "valid": sum(r["valid"] for r in self.rows),
                      "correct": sum(r["correct"] for r in self.rows)}
            result["accuracy"] = result["correct"] / result["attempts"]
            result["failures"] = result["attempts"] - result["valid"]
            dump(self.directory / "summary.json", result)
            return result
        finally:
            self.raw.close()


async def run(args):
    all_items = json.loads((EXP / f"{args.scenario}-requests.json").read_text())
    if args.mode == "quality":
        items = [i for i in all_items if i["group"] == "quality"]
    else:
        items = [i for i in all_items if i["group"] == "tune"] or all_items
    encodings = {i["request_id"]: i for i in json.loads((EXP / "encoding-reference.json").read_text())}
    async with httpx.AsyncClient(
        base_url=args.url, timeout=120, trust_env=False,
        limits=httpx.Limits(max_connections=2048, max_keepalive_connections=2048)
    ) as client:
        runner = Runner(client, Path(args.output), encodings)
        if args.mode == "quality":
            result = await runner.quality(items, args.concurrency)
        else:
            result = await runner.fixed_rate(items, args.rps, args.seconds)
        print(json.dumps({k: v for k, v in result.items() if k != "backlog_samples"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18450")
    parser.add_argument("--scenario", required=True, choices=["risk", "sop"])
    parser.add_argument("--mode", default="rate", choices=["rate", "quality"])
    parser.add_argument("--rps", type=float, default=100)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
