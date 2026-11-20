"""CPU checks for workload isolation and honest overload accounting."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from batch100_prepare import oracle
from batch100_benchmark import summary


class Batch100Tests(unittest.TestCase):
    def test_risk_partitions_and_priority(self):
        items = json.loads((ROOT / "experiments/batch100-20261120/risk-requests.json").read_text())
        partitions = {}
        for item in items:
            features = item["request"]["state"]["trusted_features"]
            self.assertEqual(sum(map(len, features.values())), 200)
            self.assertEqual(oracle(features), item["expected"]["action"])
            partitions.setdefault(item["group"], set()).add(item["case_id"])
        self.assertFalse(partitions["tune"] & partitions["quality"])
        self.assertEqual(len(partitions["quality"]), 120)
        features["request"]["signature_valid"] = False
        features["transaction"]["operation_already_executed"] = True
        self.assertEqual(oracle(features), "deny")

    def test_sop_payloads_unchanged(self):
        original = json.loads((ROOT / "experiments/family-20261120/requests.json").read_text())
        actual = json.loads((ROOT / "experiments/batch100-20261120/sop-requests.json").read_text())
        self.assertEqual(len(actual), 104)
        for before, after in zip(original, actual, strict=True):
            self.assertEqual(before["request"], after["request"])
            self.assertEqual(before["expected"], after["expected"])

    def test_overload_not_hidden_by_valid_latency(self):
        rows = [{"valid": True, "correct": True, "latency_ms": 100, "send_lag_ms": 0,
                 "response": {"usage": {"input_tokens": 1000},
                              "metrics": {"batch_size": 1, "peak_allocated_gib": 10}}},
                {"valid": False, "correct": False, "latency_ms": 10000, "send_lag_ms": 5000}]
        result = summary(rows, 10)
        self.assertEqual(result["p95_ms"], 100)
        self.assertEqual(result["all_attempts_p95_ms"], 10000)
        self.assertEqual(result["accuracy_all_attempts"], .5)
        self.assertEqual(result["valid_within_500ms"], 1)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["valid_rps"], .1)


if __name__ == "__main__":
    unittest.main()
