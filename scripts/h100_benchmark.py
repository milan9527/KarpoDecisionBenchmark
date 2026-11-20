"""Measure one predeclared Karpo configuration with frozen full-200 English requests."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx

from benchmark import Runner

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/h100-4b-20261120"


async def main(config_id, url):
    manifest = json.loads((EXP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    config = next(c for c in manifest["configs"] if c["id"] == config_id)
    items = json.loads((EXP / "requests.json").read_text())
    quality = [r for r in items if r["group"] == "synthetic"]
    boundary = [r for r in items if r["group"] == "boundary"]
    performance = [r for r in quality if r["order"] == "original"][::5]
    assert len(performance) == manifest["warmup"]
    result = EXP / "results" / config_id
    async with httpx.AsyncClient(
        base_url=url, timeout=60, trust_env=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=40),
    ) as client:
        response = await client.get("/healthz")
        response.raise_for_status()
        (result / "health.json").write_text(json.dumps(response.json(), indent=2) + "\n")
        runner = Runner(client, result)
        try:
            await runner.serial("warmup-excluded", performance)
            await runner.serial("quality-existing", quality)
            await runner.serial("quality-boundary", boundary)
            for count in config["concurrency"]:
                summary = await runner.closed_loop(
                    f"concurrency-{count}", performance, count, manifest["concurrency_requests"])
                if summary["failures"]:
                    raise RuntimeError("Invalid load responses; stop increasing concurrency.")
            (result / "complete.json").write_text(json.dumps({
                "config": config_id, "observations": runner.counter, "status": "complete",
            }, indent=2) + "\n")
        finally:
            runner.raw.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:18440")
    args = parser.parse_args()
    asyncio.run(main(args.config, args.url))
