import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare", ROOT / "scripts/prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class EvidenceTests(unittest.TestCase):
    def test_complete_candidates_and_gold(self):
        data = json.loads((ROOT / "fixtures/requests.json").read_text())
        self.assertEqual(len(data), 640)
        for row in data:
            options = row["request"]["questions"]["action"]["criteria"]
            self.assertEqual(len(options), row["candidate_count"])
            self.assertIn(row["expected"]["action"], options)
            self.assertEqual(row["request_sha256"], prepare.digest(row["request"]))
            self.assertNotIn("expected", row["request"])

    def test_order_pairs_preserve_content(self):
        data = json.loads((ROOT / "fixtures/requests.json").read_text())
        for original, reverse in zip(data[::2], data[1::2]):
            self.assertEqual(original["case_id"], reverse["case_id"])
            self.assertEqual(original["expected"], reverse["expected"])
            a = original["request"]["questions"]["action"]["criteria"]
            b = reverse["request"]["questions"]["action"]["criteria"]
            self.assertEqual(list(a), list(b)[::-1])
            self.assertEqual(a, b)

    def test_existing_payment_payload_unchanged(self):
        data = json.loads((ROOT / "fixtures/payment.json").read_text())
        self.assertEqual(len(data), 80)
        self.assertEqual(len({row["case_id"] for row in data}), 40)
        source = json.loads((ROOT / "sources/payment-source.json").read_text())
        frozen = {(r["case_id"], r["variant"]): r["request"] for r in source if r["group"] == "payment"}
        for row in data:
            self.assertEqual(row["request"], frozen[row["case_id"], row["order"]])

    def test_metrics_keep_failures_in_denominator(self):
        spec = importlib.util.spec_from_file_location("benchmark", ROOT / "scripts/benchmark.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = [{"valid": True, "correct": True, "latency_ms": 90, "response": {"usage": {"input_tokens": 7000}}},
                {"valid": False, "correct": False, "latency_ms": 500, "request_id": "failed", "error": "timeout"}]
        result = module.summarize(rows, 1)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["valid_rps"], 1)
        self.assertEqual(result["correct_within_100ms"], 1)
        self.assertEqual(result["p95_ms"], 90)


if __name__ == "__main__":
    unittest.main()
