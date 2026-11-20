"""Client-measured Karpo benchmark. Every response and failure is preserved."""
import argparse
import asyncio
import datetime as dt
import json
import math
import pathlib
import statistics
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def validate(body, item):
    answers = body["answers"]
    if set(answers) != set(item["request"]["questions"]):
        raise ValueError("Question keys differ")
    for key, question in item["request"]["questions"].items():
        answer = answers[key]
        if answer.get("type") != "choice" or answer["choice"] not in question["criteria"]:
            raise ValueError("Invalid choice")
        probs = answer["probabilities"]
        if set(probs) != set(question["criteria"]):
            raise ValueError("Missing or extra option probabilities")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values()):
            raise ValueError("Invalid probability")
        if abs(sum(probs.values()) - 1) > 0.01:
            raise ValueError("Probabilities do not sum to one")
    return all(answers[key]["choice"] == gold for key, gold in item["expected"].items())


def summarize(rows, elapsed):
    valid = [r for r in rows if r["valid"]]
    latencies = [r["latency_ms"] for r in valid]
    tokens = [r["response"].get("usage", {}).get("input_tokens") for r in valid]
    tokens = [x for x in tokens if isinstance(x, int)]
    return {
        "attempts": len(rows), "valid": len(valid), "failures": len(rows) - len(valid),
        "correct": sum(r.get("correct", False) for r in rows),
        "correct_within_100ms": sum(r.get("correct", False) and r["latency_ms"] <= 100 for r in rows),
        "latency_scope": "Scheduled send to complete response for open-loop; invocation to complete response otherwise. Local loopback HTTP.",
        "elapsed_seconds": elapsed, "valid_rps": len(valid) / elapsed if elapsed else None,
        "mean_ms": statistics.mean(latencies) if latencies else None,
        "p50_ms": percentile(latencies, .5), "p95_ms": percentile(latencies, .95),
        "p99_ms": percentile(latencies, .99), "max_ms": max(latencies) if latencies else None,
        "input_tokens_min": min(tokens) if tokens else None,
        "input_tokens_max": max(tokens) if tokens else None,
        "failure_details": [{"id": r["request_id"], "error": r.get("error"), "http_status": r.get("http_status")}
                            for r in rows if not r["valid"]],
    }


class Runner:
    def __init__(self, client, output):
        self.client = client
        self.output = output
        self.raw = (output / "requests.jsonl").open("x")
        self.counter = 0
        self.summaries = {}

    def record(self, row):
        self.raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.raw.flush()

    async def call(self, item, stage, scheduled=None):
        begin = time.perf_counter()
        scheduled = begin if scheduled is None else scheduled
        self.counter += 1
        row = {"sequence": self.counter, "stage": stage, "request_id": item["id"],
               "case_id": item["case_id"], "group": item["group"], "order": item["order"],
               "candidate_count": item["candidate_count"], "description": item["description"],
               "request_sha256": item["request_sha256"], "expected": item["expected"],
               "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "send_lag_ms": max(0, (begin - scheduled) * 1000), "valid": False, "correct": False}
        try:
            response = await self.client.post("/v1/systemone", json=item["request"])
            row["http_status"] = response.status_code
            row["server_timing"] = response.headers.get("server-timing")
            row["response"] = response.json()
            response.raise_for_status()
            row["correct"] = validate(row["response"], item)
            row["valid"] = True
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        end = time.perf_counter()
        row["latency_ms"] = (end - scheduled) * 1000
        row["http_latency_ms"] = (end - begin) * 1000
        self.record(row)
        return row

    def finish(self, stage, rows, begin):
        result = summarize(rows, time.perf_counter() - begin)
        self.summaries[stage] = result
        (self.output / "summary.json").write_text(json.dumps(self.summaries, indent=2, ensure_ascii=False) + "\n")
        print(stage, json.dumps(result, ensure_ascii=False), flush=True)
        return result

    async def serial(self, stage, items):
        begin = time.perf_counter()
        rows = []
        for item in items:
            rows.append(await self.call(item, stage))
            if len(rows) >= 3 and not any(r["valid"] for r in rows[-3:]):
                self.finish(stage, rows, begin)
                raise RuntimeError("Three consecutive invalid responses; stopping rather than continuing a failed run")
        return self.finish(stage, rows, begin)

    async def closed_loop(self, stage, items, concurrency, count):
        begin = time.perf_counter()
        pending = iter(range(count))
        rows = []

        async def worker():
            for index in pending:
                rows.append(await self.call(items[index % len(items)], stage))

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        return self.finish(stage, rows, begin)

    async def open_loop(self, stage, items, qps, seconds):
        begin = time.perf_counter()
        jobs = []
        rows = []
        count = int(qps * seconds)
        for index in range(count):
            scheduled = begin + index / qps
            await asyncio.sleep(max(0, scheduled - time.perf_counter()))
            active = sum(not job.done() for job in jobs)
            if active >= 32:
                item = items[index % len(items)]
                row = {"stage": stage, "request_id": item["id"], "valid": False, "correct": False,
                       "latency_ms": (time.perf_counter() - scheduled) * 1000,
                       "error": "client_inflight_limit_32", "scheduled_offset_s": index / qps}
                self.record(row)
                rows.append(row)
            else:
                jobs.append(asyncio.create_task(self.call(items[index % len(items)], stage, scheduled)))
        rows.extend(await asyncio.gather(*jobs))
        # Include the complete offered-load window, even if the last request completed early.
        await asyncio.sleep(max(0, begin + seconds - time.perf_counter()))
        result = self.finish(stage, rows, begin)
        result.update(offered_qps=qps, offered_seconds=seconds, planned_requests=count)
        (self.output / "summary.json").write_text(json.dumps(self.summaries, indent=2, ensure_ascii=False) + "\n")
        return result


async def run(args):
    output = ROOT / "results" / args.name
    output.mkdir(parents=True, exist_ok=False)
    fixtures = json.loads((ROOT / "fixtures/requests.json").read_text())
    payment = json.loads((ROOT / "fixtures/payment.json").read_text())
    manifest = json.loads((ROOT / "fixtures/manifest.json").read_text())
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=40)
    async with httpx.AsyncClient(base_url=args.url, timeout=30, limits=limits, trust_env=False) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        (output / "health.json").write_text(json.dumps(health.json(), indent=2) + "\n")
        (output / "plan.json").write_text(json.dumps({
            "name": args.name, "base_url": args.url, "cache": "disabled", "manifest": manifest,
            "precision_flags": args.flags, "latency_client": "same GPU host, loopback HTTP",
            "clock": "time.perf_counter", "timeout_s": 30, "automatic_retries": 0,
            "requested_phase": args.phase,
        }, indent=2, ensure_ascii=False) + "\n")
        runner = Runner(client, output)
        warm = [payment[0]] + [next(x for x in fixtures if x["candidate_count"] == n and
                                  x["description"] == length and x["order"] == "original")
                               for n in [20, 200] for length in ["compact", "detailed"]]
        await runner.serial("warmup-excluded", warm * 2)
        if args.phase == "smoke":
            runner.raw.close()
            return
        if args.phase in ["all", "quality"]:
            await runner.serial("quality-existing-payment", payment)
            selected = [x for x in fixtures if x["candidate_count"] == 200 and x["description"] == "detailed"]
            await runner.serial("quality-200-detailed", selected)
        if args.phase in ["all", "performance"]:
            for size in [20, 50, 100, 200]:
                for length in ["compact", "detailed"]:
                    items = [x for x in fixtures if x["candidate_count"] == size and
                             x["description"] == length and x["order"] == "original"][::10]
                    await runner.serial(f"size-{size}-{length}", items * 2)
            items = [x for x in fixtures if x["candidate_count"] == 200 and
                     x["description"] == "detailed" and x["order"] == "original"][::5]
            for concurrency in [1, 2, 4, 8]:
                result = await runner.closed_loop(f"concurrency-{concurrency}", items, concurrency, 32)
                if result["failures"]:
                    raise RuntimeError("Load test failure; do not increase load while server may be draining")
            for qps in [1, 2, 4, 8]:
                result = await runner.open_loop(f"arrival-{qps}-qps", items, qps, 5)
                if result["failures"] or (result["p95_ms"] or 0) > 5000:
                    break
        runner.raw.close()
        (output / "complete.json").write_text(json.dumps({"completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                          "total_observations": runner.counter}, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:18440")
    p.add_argument("--name", required=True)
    p.add_argument("--flags", default="BF16")
    p.add_argument("--phase", choices=["all", "smoke", "quality", "performance"], default="all")
    asyncio.run(run(p.parse_args()))
