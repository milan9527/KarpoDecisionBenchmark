import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from promo_opt_calibration import decide


class ReviewThresholdTests(unittest.TestCase):
    def test_only_allow_can_be_escalated(self):
        for action in ("deny", "deduplicate", "throttle", "challenge", "review"):
            answer = {"choice": action, "probabilities": {"review": .3, "allow": .7}}
            self.assertEqual(decide(answer, .1)["action"], action)

    def test_threshold_boundary_and_unchanged_raw_answer(self):
        answer = {"choice": "allow", "probabilities": {"review": .2, "allow": .8}}
        original = copy.deepcopy(answer)
        self.assertEqual(decide(answer, .2)["action"], "review")
        self.assertEqual(decide(answer, .200001)["action"], "allow")
        self.assertEqual(answer, original)

    def test_unknown_or_invalid_scores_are_not_silently_used(self):
        with self.assertRaises(ValueError):
            decide({"choice": "allow", "probabilities": {"review": float("nan"), "allow": .8}}, .1)


if __name__ == "__main__":
    unittest.main()
