import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "experiments/concise-fp8-20261119/fixtures"


class ConciseInputTests(unittest.TestCase):
    def test_every_variant_keeps_all_candidates_and_exact_business_state(self):
        rows = json.loads((FIXTURES / "requests.json").read_text())
        by_variant = {}
        for row in rows:
            by_variant.setdefault(row["description"], {})[row["case_id"], row["order"]] = row
        original_source = {
            (r["case_id"], r["order"]): r["request"]
            for r in json.loads((ROOT / "fixtures/requests.json").read_text())
            if r["candidate_count"] == 200 and r["description"] == "detailed"
        }
        self.assertEqual(set(by_variant), {"original", "concise-en", "concise-zh", "factored"})
        for key, baseline in by_variant["original"].items():
            if key in original_source:
                self.assertEqual(baseline["request"], original_source[key])
            for variant, cases in by_variant.items():
                row = cases[key]
                self.assertEqual(len(cases), 104)
                self.assertEqual(row["expected"], baseline["expected"])
                self.assertEqual(row["request"]["state"], baseline["request"]["state"])
                options = row["request"]["questions"]["action"]["criteria"]
                self.assertEqual(len(options), 200)
                self.assertEqual(list(options), list(baseline["request"]["questions"]["action"]["criteria"]))
                self.assertNotIn("expected", row["request"])

    def test_factored_options_resolve_to_exact_original_policies(self):
        catalog = json.loads((ROOT / "fixtures/catalog.json").read_text())
        specs = json.loads((FIXTURES / "action-specs.json").read_text())
        descriptions = json.loads((FIXTURES / "description-variants.json").read_text())
        rows = json.loads((FIXTURES / "requests.json").read_text())
        instruction = next(r["request"]["questions"]["action"]["instructions"]
                           for r in rows if r["description"] == "factored")
        domains = list(dict.fromkeys(c["domain"] for c in catalog))
        self.assertEqual(len(domains), 10)
        self.assertEqual(len(specs), 20)
        for candidate in catalog:
            di, ai = map(int, re.fullmatch(r"D(\d+) A(\d+)", descriptions[candidate["id"]]["factored"]).groups())
            self.assertEqual(domains[di], candidate["domain"])
            self.assertEqual(specs[ai]["action"], candidate["action"])
            original_condition = candidate["detailed"].split("Use when: ", 1)[1]
            self.assertEqual(specs[ai]["original_condition"], original_condition)
            self.assertIn(f"A{ai}={specs[ai]['original_name']}. Use when: {original_condition}", instruction)
