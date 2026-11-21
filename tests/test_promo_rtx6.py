"""Routing must cover replicas 4/5 and preserve the calibrated response contract."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from promo_opt_calibrated_proxy import ReviewPolicy
from vllm_rps_proxy import make_app


class SixReplicaTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_six_replicas_preserve_input_and_raw_answer(self):
        body = {"state": {"trusted_features": {"feature": 200}}, "questions": {"action": {"type": "choice"}}}
        answer = {"type": "choice", "choice": "allow", "confidence": .8,
                  "probabilities": {"allow": .8, "review": .19, "deny": .01,
                                    "throttle": 0, "challenge": 0, "deduplicate": 0}}
        received = []

        async def backend(request):
            replica = int(request.url.host.removeprefix("replica"))
            received.append((replica, json.loads(request.content)))
            await asyncio.sleep(.01)
            return httpx.Response(200, json={"answers": {"action": answer}, "metrics": {"replica_id": replica}})

        app = make_app([f"http://replica{i}" for i in range(6)])
        app.state.calibration = (.1628260940455841, "frozen")
        app.add_middleware(ReviewPolicy)
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as backend_client:
            app.state.client = backend_client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                results = await asyncio.gather(*(client.post("/v1/systemone", json=body) for _ in range(6)))
        self.assertEqual(sorted(r for r, _ in received), list(range(6)))
        self.assertTrue(all(b == body for _, b in received))
        for response in results:
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["answers"]["action"], answer)
            self.assertEqual(result["decision"]["action"], "review")
            self.assertEqual(result["metrics"]["replica_id"], result["metrics"]["proxy_replica"])


if __name__ == "__main__":
    unittest.main()
