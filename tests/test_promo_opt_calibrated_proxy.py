import copy
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from promo_opt_calibrated_proxy import ReviewPolicy
from vllm_rps_proxy import make_app


class CalibratedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_request_and_raw_answer_and_returns_separate_decision(self):
        answer = {"type": "choice", "choice": "allow", "confidence": .8,
                  "probabilities": {"allow": .8, "review": .19, "deny": .01,
                                    "throttle": 0, "challenge": 0, "deduplicate": 0}}
        received = []

        def backend(request):
            received.append(json.loads(request.content))
            return httpx.Response(200, json={"answers": {"action": answer}, "metrics": {"replica_id": 0}})

        app = make_app(["http://backend.invalid"])
        app.state.calibration = (.1628260940455841, "frozen-test-hash")
        app.add_middleware(ReviewPolicy)
        body = {"state": {"trusted_features": {"test": 200}}, "questions": {"action": {"type": "choice"}}}
        original = copy.deepcopy(body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
            app.state.client = client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as front:
                response = await front.post("/v1/systemone", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(received, [original])
        payload = response.json()
        self.assertEqual(payload["answers"]["action"], answer)
        self.assertEqual(payload["decision"]["action"], "review")
        self.assertEqual(payload["decision"]["raw_action"], "allow")
        self.assertEqual(payload["decision"]["calibration_sha256"], "frozen-test-hash")


if __name__ == "__main__":
    unittest.main()
