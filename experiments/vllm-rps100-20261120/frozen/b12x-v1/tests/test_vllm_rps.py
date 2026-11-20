import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vllm_rps_benchmark import Runner, summarize


class FixedArrivalTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_responses_do_not_reduce_offered_rate(self):
        starts = []
        loop = asyncio.get_running_loop()

        async def handler(request):
            starts.append(loop.time())
            await asyncio.sleep(.25)
            return httpx.Response(429, json={"error": "full"})

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
                runner = Runner(client, Path(directory) / "run")
                result = await runner.fixed_rate(
                    [{"id": "test", "request_sha256": "test", "expected": {}, "request": {}}],
                    100, .1,
                )
        self.assertEqual(result["attempts"], 10)
        self.assertEqual(result["failures"], 10)
        self.assertLess(starts[-1] - starts[0], .2)
        self.assertFalse(result["passes_performance_acceptance"])

    def test_fast_rejections_never_pass_latency_acceptance(self):
        rows = [
            {"valid": False, "correct": False, "scheduled_offset_s": n / 100,
             "send_offset_s": n / 100, "end_offset_s": n / 100 + .001,
             "latency_ms": 1, "http_latency_ms": 1, "send_lag_ms": 0, "http_status": 429}
            for n in range(12000)
        ]
        result = summarize(rows, 100, 120, 120)
        self.assertEqual(result["failures"], 12000)
        self.assertIsNone(result["p50_ms"])
        self.assertFalse(result["passes_performance_acceptance"])


if __name__ == "__main__":
    unittest.main()
