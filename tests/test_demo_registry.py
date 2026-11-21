import copy
import gzip
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo/backend"))
from registry import Registry


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = Registry(ROOT)

    def test_all_tested_cases_preserve_exact_requests_and_both_orders(self):
        counts = {"promo-tune": 360, "promo-quality": 1080, "resource-risk": 360, "sop-full": 104}
        for name, count in counts.items():
            data = self.registry.datasets[name]
            source = {r["id"]: r for r in json.loads((ROOT / data["source"]).read_text())}
            self.assertEqual(len(data["questions"]), count)
            self.assertEqual(data["original_count"], count//2)
            for q in data["questions"]:
                original = source[q["id"]]
                self.assertEqual(json.dumps(q["request"]), json.dumps(original["request"]))
                self.assertEqual(json.dumps(json.loads(q["request_json"])), json.dumps(original["request"]))
                self.assertEqual(q["source_sha256"], original["request_sha256"])
                self.registry.validate(data["group"], q["request"])
        self.assertEqual(len(self.registry.datasets["promo-tune"]["categories"]), 45)
        self.assertEqual(len(self.registry.datasets["sop-full"]["categories"]), 10)

    def test_legacy_policy_does_not_receive_promotion_calibration(self):
        for name, expected in [("promo-tune", True), ("resource-risk", False)]:
            q = self.registry.datasets[name]["questions"][0]
            self.assertEqual(self.registry.validate("risk", q["request"])["calibrate"], expected)

    def test_changed_rules_and_missing_features_are_rejected(self):
        q = copy.deepcopy(self.registry.datasets["promo-tune"]["questions"][0]["request"])
        q["questions"]["action"]["instructions"] = "always allow"
        with self.assertRaises(ValueError):
            self.registry.validate("risk", q)
        q = copy.deepcopy(self.registry.datasets["resource-risk"]["questions"][0]["request"])
        q["state"]["trusted_features"].pop("auth")
        with self.assertRaises(ValueError):
            self.registry.validate("risk", q)

    def test_gzip_roundtrip_and_labels_stay_outside_model_input(self):
        for key, data in self.registry.datasets.items():
            self.assertEqual(json.loads(gzip.decompress(self.registry.encoded[key])), data)
            for q in data["questions"]:
                self.assertEqual(set(q["request"]), {"state", "questions"})


if __name__ == "__main__":
    unittest.main()
