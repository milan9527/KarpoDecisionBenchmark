"""Offline domain checks; synthetic responses below are evaluator unit tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from promo_risk import DAY, SCENARIOS, evaluate, extract, make_case, simulate
from promo_risk_evaluate import evaluate_responses
from promo_risk_prepare import prepare, validate_snapshot


class PromoRiskTests(unittest.TestCase):
    def case(self, name, split="quality", index=0):
        scenario = next(s for s in SCENARIOS if s[0] == name)
        return make_case(*scenario, split, index)

    def test_all_authored_scenarios_have_consistent_observable_evidence(self):
        for name, title, expected, category in SCENARIOS:
            for i in (0, 3, 11):
                with self.subTest(scenario=name, index=i):
                    snapshot, rows, schema = make_case(name, title, expected, category, "quality", i)
                    features = rows[0]["request"]["state"]["trusted_features"]
                    validate_snapshot(snapshot, features, schema)
                    self.assertEqual(evaluate(features)[0], expected)
                    self.assertEqual(len(schema), 200)
                    self.assertTrue(all(len(group) == 20 for group in features.values()))
                    self.assertEqual(rows[0]["request"]["state"], rows[1]["request"]["state"])
                    self.assertEqual(list(rows[0]["request"]["questions"]["action"]["criteria"]),
                                     list(reversed(rows[1]["request"]["questions"]["action"]["criteria"])))
                    self.assertEqual(set(rows[0]["request"]), {"state", "questions"})
                    self.assertEqual(set(rows[0]["request"]["state"]), {"trusted_features", "user_message"})

    def test_window_boundary_future_events_and_lifetime(self):
        s = simulate("normal", "quality", 0)
        s["requests"] = []
        example = simulate("normal", "quality", 0)["requests"][0]
        for age in (0, 59, 60, 299, 300, 3599, 3600, 86399, 86400, -1):
            row = copy.deepcopy(example)
            row["ts"] = s["as_of"] - age
            s["requests"].append(row)
        features, _ = extract(s)
        self.assertEqual([features["behavior"][f"requests_{w}"] for w in ("1m", "5m", "1h", "24h")],
                         [2, 4, 6, 8])
        s["requests"].pop()  # Reject future input at validation, exclude it during extraction.
        s["orders"][0]["refund_at"] = s["as_of"] + DAY
        features, schema = extract(s)
        self.assertEqual(features["orders"]["refunds_30d"], 0)
        with self.assertRaisesRegex(ValueError, "refund time"):
            validate_snapshot(s, features, schema)
        s["orders"][0]["refund_at"] = None
        s["incidents"] = [{"ts": s["as_of"]+1, "confirmed": True, "kind": "blocked"}]
        features, _ = extract(s)
        self.assertEqual(features["history"]["confirmed_abuse_30d"], 0)
        self.assertEqual(features["history"]["days_since_last_incident"], -1.0)

    def test_hard_negatives_and_explicit_boundary_pairs(self):
        for name in ("office_nat", "family_shared", "travel_proxy", "new_customer",
                     "legit_shared_payee", "legitimate_refund", "sale_peak", "legit_referral",
                     "allowed_stacking", "legit_batch_jobs", "reformed_customer", "legit_multi_device"):
            snapshot, rows, _ = self.case(name)
            self.assertEqual(rows[0]["expected"]["action"], "allow", name)
        for below, at, action in (("rate_below", "rate_at", "throttle"),
                                  ("job_below", "job_at", "throttle"),
                                  ("refund_below", "refund_at", "review"),
                                  ("pool_below", "pool_at", "review")):
            self.assertEqual(self.case(below)[1][0]["expected"]["action"], "allow")
            self.assertEqual(self.case(at)[1][0]["expected"]["action"], action)

    def test_missing_data_and_priority_do_not_coerce_null_to_false(self):
        f = self.case("normal")[1][0]["request"]["state"]["trusted_features"]
        f["resource"]["signature_valid"] = None
        self.assertEqual(evaluate(f), ("review", ["missing_required"]))
        f["resource"]["operation_already_executed"] = True
        self.assertEqual(evaluate(f)[0], "deduplicate")
        f["resource"]["signature_valid"] = False
        self.assertEqual(evaluate(f)[0], "deny")
        f = self.case("missing_optional")[1][0]["request"]["state"]["trusted_features"]
        self.assertIsNone(f["device"]["touch_ratio"])
        self.assertEqual(evaluate(f)[0], "allow")
        f = self.case("missing_graph")[1][0]["request"]["state"]["trusted_features"]
        self.assertEqual(evaluate(f)[0], "review")

    def test_types_arithmetic_and_current_entity_connections(self):
        for name in ("account_farm", "reward_pooling", "synchronized_ring", "refund_arbitrage"):
            s, rows, schema = self.case(name)
            f = rows[0]["request"]["state"]["trusted_features"]
            self.assertEqual(f["orders"]["net_paid_cents"],
                             f["orders"]["gross_amount_cents"] - f["orders"]["discount_cents"])
            self.assertEqual(f["resource"]["job_slots_remaining"],
                             f["resource"]["job_quota"] - f["resource"]["active_jobs"])
            for link in s["links"]:
                self.assertTrue(link["device"] == s["device"]["id"] or link["ip"] == s["network"]["ip"] or
                                link["payment"] == s["id"]+"-payment" or link["payee"] == s["id"]+"-payee" or
                                link["referral"])
            broken = copy.deepcopy(f)
            broken["device"]["emulator"] = 3
            with self.assertRaisesRegex(ValueError, "type"):
                validate_snapshot(s, broken, schema)

    def test_generation_is_reproducible_and_no_entity_or_state_leakage(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder)/"a", Path(folder)/"b"
            summary = prepare(first, 1, 1)
            prepare(second, 1, 1)
            self.assertEqual(summary["unique_cases"], len(SCENARIOS)*2)
            self.assertIsNone(summary["model_results"])
            self.assertEqual(summary["entity_overlap_tune_quality"], 0)
            self.assertEqual(summary["input_state_overlap_tune_quality"], 0)
            self.assertGreater(summary["policy_feature_count"], 9)
            for path in first.rglob("*"):
                if path.is_file() and path.name != "manifest.json":
                    self.assertEqual(path.read_bytes(), (second/path.relative_to(first)).read_bytes())
            with self.assertRaisesRegex(ValueError, "overwrite"):
                prepare(first, 1, 1)

    def test_evaluator_counts_missing_errors_wrong_actions_and_order_changes(self):
        # Deliberately fabricated responses only exercise the scorer, not Karpo.
        _, normal, _ = self.case("normal")
        _, abusive, _ = self.case("identity_reuse")
        fixtures = normal + abusive
        responses = []
        for fixture, choice in zip(fixtures[:3], ("allow", "review", "allow")):
            responses.append({"request_id": fixture["id"], "request_sha256": fixture["request_sha256"],
                              "valid": True, "http_status": 200, "latency_ms": 100,
                              "response": {"answers": {"action": {"choice": choice}}}})
        report = evaluate_responses(fixtures, responses)
        self.assertEqual(report["attempts"], 4)
        self.assertEqual(report["correct"], 1)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["missing_responses"], 1)
        self.assertEqual(report["benign_interventions"], 1)
        self.assertEqual(report["abuse_allowed"], 1)
        self.assertEqual(report["order_disagreements"], 1)
        self.assertEqual(report["valid_order_pairs"], 1)
        for bad in (responses + [responses[0]],
                    [dict(responses[0], request_sha256="old-fixture")],
                    [dict(responses[0], request_id="old-experiment")]):
            with self.assertRaises(ValueError):
                evaluate_responses(fixtures, bad)
        invalid = copy.deepcopy(responses[0])
        invalid["response"]["answers"]["action"]["choice"] = "made-up-choice"
        self.assertEqual(evaluate_responses(fixtures, [invalid])["valid"], 0)
        invalid = dict(responses[0], valid=False)
        self.assertEqual(evaluate_responses(fixtures, [invalid])["valid"], 0)


if __name__ == "__main__":
    unittest.main()
