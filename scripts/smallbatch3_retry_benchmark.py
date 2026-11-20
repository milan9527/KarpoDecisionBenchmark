"""Frozen original-input experiment: batch 1/2/4, replicas 1/2/3, C100."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx
import batch100_benchmark as bench

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/smallbatch3-retry-20261120"
bench.EXP = EXP


async def main():
    manifest = json.loads((EXP / "manifest.json").read_text())
    target = EXP / "results/measurements"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18451", timeout=180, trust_env=False,
                                limits=httpx.Limits(max_connections=150, max_keepalive_connections=150)) as client:
        response = await client.get("/healthz")
        response.raise_for_status()
        bench.write(target / "health.json", response.json())
        for scenario in ("risk", "sop"):
            quality, tuning = bench.items_for(scenario)
            base = target / scenario
            choices = []
            # Alternating batch order limits a consistent time-order advantage.
            for replicas in manifest["replicas"]:
                batches = manifest["batch_sizes"] if replicas != 2 else list(reversed(manifest["batch_sizes"]))
                for batch in batches:
                    directory = base / f"r{replicas}-b{batch}-w0"
                    await bench.configure(client, replicas, batch, 0)
                    await bench.measure(client, directory / "warmup", tuning, concurrency=replicas * batch,
                                        count=replicas * batch * 4)
                    quality_result = await bench.measure(client, directory / "quality", quality,
                                                         concurrency=100, count=len(quality))
                    bench.parity(ROOT / "experiments/batch100-20261120/results/fp8" /
                                 scenario / "quality-serial", directory / "quality")
                    result = await bench.measure(client, directory / "sustained-100", tuning,
                                                 concurrency=100, seconds=manifest["sustained_seconds"])
                    choices.append({"replicas": replicas, "batch": batch, "wait_ms": 0,
                                    "quality": quality_result, "performance": result})
                    bench.write(base / "matrix.json", choices)
            # Select solely on latency among successful configurations meeting the
            # synthetic quality threshold. Repeat the three-GPU winner once.
            candidates = [c for c in choices if c["replicas"] == 3 and
                          c["performance"]["failures"] == 0 and c["quality"]["failures"] == 0 and
                          c["quality"]["accuracy_all_attempts"] > .90]
            if not candidates:
                raise RuntimeError(f"No eligible three-GPU configuration for {scenario}")
            chosen = min(candidates, key=lambda c: (c["performance"]["p50_ms"],
                                                   -c["performance"]["valid_rps"]))
            bench.write(base / "selection.json", {"selected": chosen, "all_configs": choices})
            await bench.configure(client, 3, chosen["batch"], 0)
            await bench.measure(client, base / "confirm-warmup", tuning,
                                concurrency=3 * chosen["batch"], count=12 * chosen["batch"])
            await bench.measure(client, base / "confirm-100", tuning,
                                concurrency=100, seconds=manifest["sustained_seconds"])
    bench.write(target / "complete.json", {"status": "complete"})


if __name__ == "__main__":
    asyncio.run(main())
