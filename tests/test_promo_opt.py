"""Guard old-engine equivalence and token budget at the new batch sizes."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from promo_opt_server import graph_config


class GraphConfigTest(unittest.TestCase):
    def test_old_graph_is_restored_exactly(self):
        self.assertEqual(graph_config({2304}, 4, "powers"), (32768, [2304, 4608, 9216]))

    def test_dense_adds_intermediate_batch(self):
        self.assertEqual(graph_config({3328}, 4, "dense"),
                         (32768, [3328, 6656, 9984, 13312]))

    def test_sixteen_full_inputs_fit(self):
        budget, captures = graph_config({3328}, 16, "dense")
        self.assertEqual(captures, [3328 * i for i in range(1, 17)])
        self.assertLessEqual(captures[-1], budget)

    def test_long_inputs_cannot_exceed_token_budget(self):
        budget, captures = graph_config({8192}, 16, "dense")
        self.assertTrue(all(i <= budget for i in captures))
        self.assertEqual(captures[-1], 65536)


if __name__ == "__main__":
    unittest.main()
