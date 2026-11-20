"""Run in the isolated vLLM environment; no GPU execution is required."""

from types import SimpleNamespace
import unittest


class MarkerPoolingTests(unittest.TestCase):
    def test_ragged_sequences_read_markers_and_keep_suffixes(self):
        import torch
        from karpo_vllm_model import MarkerPool

        hidden = torch.arange(18, dtype=torch.float32).reshape(9, 2)
        params = [
            SimpleNamespace(extra_kwargs={"karpo_marker": 1}),
            SimpleNamespace(extra_kwargs={"karpo_marker": 2}),
        ]
        cursor = SimpleNamespace(
            is_partial_prefill=lambda: False,
            prompt_lens_cpu=torch.tensor([4, 5]),
            first_token_indices_gpu=torch.tensor([0, 4]),
        )
        metadata = SimpleNamespace(
            get_pooling_cursor=lambda: cursor, pooling_params=params
        )
        result = MarkerPool()(hidden, metadata)
        self.assertTrue(torch.equal(result, hidden[[1, 6]]))
        self.assertFalse(torch.equal(result, hidden[[3, 8]]))
        self.assertEqual(hidden.shape[0], 9)

    def test_rejects_partial_bidirectional_input(self):
        import torch
        from karpo_vllm_model import MarkerPool

        metadata = SimpleNamespace(
            get_pooling_cursor=lambda: SimpleNamespace(is_partial_prefill=lambda: True)
        )
        with self.assertRaisesRegex(RuntimeError, "complete"):
            MarkerPool()(torch.zeros(3, 2), metadata)

    def test_rejects_invalid_marker_offsets(self):
        from karpo_vllm_model import marker_offsets

        for marker in (-1, 4, 1.5):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                marker_offsets([SimpleNamespace(extra_kwargs={"karpo_marker": marker})], [4])


class InflightTests(unittest.TestCase):
    def test_integrates_overlap_without_using_p50(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from vllm_single_benchmark import inflight

        rows = [
            {"start_offset_s": 0, "send_lag_ms": 0, "latency_ms": 3000},
            {"start_offset_s": 1, "send_lag_ms": 0, "latency_ms": 3000},
        ]
        self.assertEqual(inflight(rows, 0, 4), {"peak": 2, "mean_10_to_60s": 1.5})


if __name__ == "__main__":
    unittest.main()
