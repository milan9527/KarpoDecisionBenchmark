"""Pre-benchmark compatibility gate; these observations are not performance data."""

import asyncio
import json
from pathlib import Path
import time

import httpx

from benchmark import validate
from batch100_benchmark import write

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-single-20261120"


async def main():
    rows = []
    encodings = {r["request_id"]: r for r in json.loads((EXP / "results/encoding/native.json").read_text())}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18450", trust_env=False, timeout=120) as client:
        start = time.monotonic()
        while True:
            try:
                response = await client.get("/healthz", timeout=2)
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() - start > 300:
                    raise TimeoutError("Development server did not become ready")
                await asyncio.sleep(2)
        for scenario in ("risk", "sop"):
            items = [x for x in json.loads((EXP / f"{scenario}-requests.json").read_text()) if x["group"] == "quality"]
            reference = {
                r["request_id"]: r for r in map(json.loads, (
                    ROOT / "experiments/batch100-20261120/results/fp8" / scenario /
                    "quality-serial/requests.jsonl").read_text().splitlines())
            }
            # Take original/reversed pairs distributed across the entire fixture.
            selected = [items[(i * (len(items) // 2) // 8) * 2 + order]
                        for i in range(8) for order in (0, 1)]
            for item in selected:
                response = await client.post("/v1/systemone", json=item["request"])
                body = response.json()
                response.raise_for_status()
                correct = validate(body, item)
                encoded = encodings[item["id"]]
                assert body["metrics"]["tokens_sha256"] == encoded["tokens_sha256"]
                assert body["metrics"]["marker"] == encoded["marker"]
                a = body["answers"]["action"]
                b = reference[item["id"]]["response"]["answers"]["action"]
                rows.append({
                    "request_id": item["id"], "scenario": scenario, "correct": correct,
                    "same_choice": a["choice"] == b["choice"],
                    "max_probability_delta": max(abs(a["probabilities"][k] - v)
                                                 for k, v in b["probabilities"].items()),
                    "response": body,
                })
                write(EXP / "results/smoke.json", {"status": "running", "rows": rows})
        summary = {
            scenario: {
                "observations": len([r for r in rows if r["scenario"] == scenario]),
                "same_choices": sum(r["same_choice"] for r in rows if r["scenario"] == scenario),
                "correct": sum(r["correct"] for r in rows if r["scenario"] == scenario),
            } for scenario in ("risk", "sop")
        }
        passed = all(r["same_choices"] / r["observations"] >= .90 for r in summary.values())
        write(EXP / "results/smoke.json", {"status": "passed" if passed else "needs-investigation",
                                          "summary": summary, "rows": rows})
        print(json.dumps(summary), flush=True)
        assert passed, "Investigate answer differences before performance measurement"


if __name__ == "__main__":
    asyncio.run(main())
