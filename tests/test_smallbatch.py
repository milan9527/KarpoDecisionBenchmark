import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from smallbatch_proxy import Router
from smallbatch3_proxy import Router as ThreeGpuRouter


class ReplicaRoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = Router(["http://primary", "http://secondary"])
        for client in self.router.clients:
            client.close()
        self.received = []

        def handle(request):
            body = json.loads(request.content)
            self.received.append((request.url.host, request.url.path, body))
            if request.url.path == "/configure":
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"answers": {"action": {"choice": "allow"}},
                                             "metrics": {"gpu": 1}})

        self.router.clients = [httpx.Client(base_url=url, transport=httpx.MockTransport(handle))
                               for url in self.router.urls]

    def tearDown(self):
        for client in self.router.clients:
            client.close()

    def test_four_gpu_routes_preserve_original_input_and_identify_gpu(self):
        self.router.configure({"replicas": 4, "max_batch": 2, "wait_ms": 0})
        original = {"state": {"message": "保持原始输入", "features": list(range(200))},
                    "candidates": [{"id": str(i)} for i in range(200)]}
        results = [self.router.call(original) for _ in range(4)]
        calls = [r for r in self.received if r[1] == "/v1/systemone"]
        self.assertEqual([r[0] for r in calls], ["primary", "secondary", "primary", "secondary"])
        self.assertTrue(all(r[2] == original for r in calls))
        self.assertEqual([r[1]["metrics"]["global_gpu"] for r in results], [1, 3, 1, 3])
        self.assertEqual(self.router.busy, [0, 0])

    def test_one_two_gpu_modes_only_use_primary(self):
        for replicas in (1, 2):
            self.router.configure({"replicas": replicas, "max_batch": 1, "wait_ms": 0})
            self.router.call({"input": "unchanged"})
        calls = [r for r in self.received if r[1] == "/v1/systemone"]
        self.assertEqual([r[0] for r in calls], ["primary", "primary"])

    def test_backend_error_releases_inflight_count(self):
        def fail(request):
            raise httpx.ConnectError("backend unavailable", request=request)
        self.router.clients[0].close()
        self.router.clients[0] = httpx.Client(base_url="http://primary",
                                              transport=httpx.MockTransport(fail))
        with self.assertRaises(httpx.ConnectError):
            self.router.call({"input": "unchanged"})
        self.assertEqual(self.router.busy, [0, 0])


class ThreeGpuRoutingTests(unittest.TestCase):
    def test_two_plus_one_capacity_and_gpu_identity(self):
        router = ThreeGpuRouter(["http://primary", "http://secondary"])
        received = []
        for client in router.clients:
            client.close()

        def handle(request):
            received.append((request.url.host, request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"metrics": {"gpu": 0}})

        router.clients = [httpx.Client(base_url=url, transport=httpx.MockTransport(handle))
                          for url in router.urls]
        try:
            router.configure({"replicas": 3, "max_batch": 4, "wait_ms": 0})
            self.assertEqual([r[2]["replicas"] for r in received], [2, 1])
            router.busy = [1, 1]
            router.counter = 1
            # Equal host counts mean less work per GPU on the two-GPU host.
            router.call({"all_original_fields": [1, 2, 3]})
            self.assertEqual(received[-1][0], "primary")
            router.busy = [2, 0]
            _, response = router.call({"all_original_fields": [1, 2, 3]})
            self.assertEqual(response["metrics"]["global_gpu"], 2)
            self.assertEqual(router.busy, [2, 0])
        finally:
            for client in router.clients:
                client.close()


if __name__ == "__main__":
    unittest.main()
