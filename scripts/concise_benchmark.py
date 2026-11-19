"""Measure all frozen full-candidate variants on one already-running FP8 server."""
import argparse
import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
import time

import httpx

from benchmark import Runner

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/concise-fp8-20261119"


async def main(args):
    manifest = json.loads((EXP / "fixtures/manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if hashlib.sha256((EXP / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Frozen fixture changed: {name}")
    for name, expected in manifest["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Original input changed: {name}")
    items = json.loads((EXP / "fixtures/requests.json").read_text())
    results = EXP / "results"
    runners = {}
    outcomes = []
    beginning = time.monotonic()
    async with httpx.AsyncClient(base_url=args.url, timeout=30, trust_env=False,
                                 limits=httpx.Limits(max_connections=40, max_keepalive_connections=40)) as client:
        response = await client.get("/healthz")
        response.raise_for_status()
        (results / "health.json").write_text(json.dumps(response.json(), indent=2) + "\n")
        (results / "plan.json").write_text(json.dumps({
            "manifest": manifest, "url": args.url, "result_cache": False,
            "precision": "FP8 + Triton + Graphs where eligible",
            "requests_always_have_200_candidates": True, "business_routing_or_filtering": False,
            "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, indent=2) + "\n")
        try:
            for variant in manifest["variants"]:
                directory = results / variant
                directory.mkdir(exist_ok=False)
                runner = Runner(client, directory)
                runners[variant] = runner
                quality = [r for r in items if r["description"] == variant and r["group"] == "synthetic"]
                boundary = [r for r in items if r["description"] == variant and r["group"] == "boundary"]
                performance = [r for r in quality if r["order"] == "original"][::5]
                print("VARIANT", variant, flush=True)
                await runner.serial("warmup-excluded", performance)
                await runner.serial("quality-existing", quality)
                await runner.serial("quality-boundary", boundary)
                for count in manifest["performance"]["concurrency"]:
                    result = await runner.closed_loop(
                        f"concurrency-{count}", performance, count,
                        manifest["performance"]["requests_per_cell"])
                    if result["failures"]:
                        raise RuntimeError("Invalid load responses; stop increasing load.")
                outcomes.append({"variant": variant, "status": "measured",
                                 "observations": runner.counter})
                (results / "progress.json").write_text(json.dumps(outcomes, indent=2) + "\n")
            original_perf = [r for r in items if r["description"] == "original" and
                             r["group"] == "synthetic" and r["order"] == "original"][::5]
            await runners["original"].serial("baseline-drift-after", original_perf * 2)
            for entry in outcomes:
                entry["status"] = "complete"
                entry["observations"] = runners[entry["variant"]].counter
                (results / entry["variant"] / "complete.json").write_text(json.dumps({
                    "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "observations": entry["observations"],
                }, indent=2) + "\n")
        finally:
            for runner in runners.values():
                runner.raw.close()
            (results / "outcomes.json").write_text(json.dumps({
                "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "elapsed_seconds": time.monotonic() - beginning, "variants": outcomes,
            }, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18440")
    asyncio.run(main(parser.parse_args()))
