"""Complete bidirectional forward, followed by Karpo's FP32 marker readout.

D3 supplies the bare-backbone weight mapping and secondary readout loader.
Its default LAST pooling is replaced; no prompt token is removed or rewritten.
"""

import torch

from vllm.model_executor.layers.linear import ReplicatedLinear
from vllm.model_executor.layers.pooler.seqwise.methods import SequencePoolingMethod
from vllm.model_executor.layers.pooler.seqwise.poolers import pooler_for_classify
from vllm.model_executor.layers.pooler.special import DispatchPooler
from vllm.model_executor.models.d3 import D3ForSequenceClassification
from vllm.model_executor.models.interfaces_base import default_pooling_type


def marker_offsets(params, lengths):
    """Resolve within-sequence marker positions; synthetic profiling uses LAST."""
    offsets = []
    for param, length in zip(params, lengths, strict=True):
        extra = param.extra_kwargs or {}
        marker = extra.get("karpo_marker", int(length) - 1)
        if not isinstance(marker, int) or not 0 <= marker < int(length):
            raise ValueError("Invalid Karpo marker position")
        offsets.append(marker)
    return offsets


class MarkerPool(SequencePoolingMethod):
    def get_supported_tasks(self):
        return {"classify"}

    def forward(self, hidden_states, pooling_metadata):
        cursor = pooling_metadata.get_pooling_cursor()
        if cursor.is_partial_prefill():
            raise RuntimeError("Karpo requires a complete bidirectional forward")
        offsets = marker_offsets(
            pooling_metadata.pooling_params, cursor.prompt_lens_cpu.tolist()
        )
        positions = cursor.first_token_indices_gpu + torch.tensor(
            offsets, device=hidden_states.device, dtype=torch.long
        )
        return hidden_states[positions]


@default_pooling_type(seq_pooling_type="LAST")
class KarpoForSequenceClassification(D3ForSequenceClassification):
    def _init_pooler(self, vllm_config, prefix=""):
        config = vllm_config.model_config
        if config.hf_text_config.is_causal:
            raise ValueError("Karpo full attention must be bidirectional")
        self.score = ReplicatedLinear(
            config.get_hidden_size(),
            config.hf_config.num_labels,
            bias=False,
            params_dtype=torch.float32,
            quant_config=None,
            return_bias=False,
            prefix=f"{prefix}.score" if prefix else "score",
        )
        return DispatchPooler(
            {
                "classify": pooler_for_classify(
                    config.pooler_config, pooling=MarkerPool(), classifier=self.score
                )
            }
        )
