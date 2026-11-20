"""Register the benchmark adapter in the API and spawned vLLM processes."""

ARCHITECTURE = "KarpoForSequenceClassification"


def register():
    from vllm import ModelRegistry
    from vllm.model_executor.models.config import (
        MODELS_CONFIG_MAP,
        Qwen3_5ForConditionalGenerationConfig,
    )

    class KarpoConfig(Qwen3_5ForConditionalGenerationConfig):
        @staticmethod
        def verify_and_update_model_config(model_config):
            import json
            from pathlib import Path

            config = model_config.hf_config
            decision = json.loads(
                (Path(model_config.model) / "decision_config.json").read_text()
            )
            if (
                decision["format_version"] != 1
                or decision["attention"] != "bidirectional"
                or decision["template"] != "karpo-joint-preview-v1"
                or decision["marker"] != "<|fim_suffix|>"
                or decision["readout_dtype"] != "float32"
                or decision.get("backbone") is not None
            ):
                raise ValueError("Unsupported Karpo checkpoint contract")
            if len(decision["codes"]) != len(decision["token_ids"]):
                raise ValueError("Inconsistent Karpo answer codes")
            config.is_causal = False
            model_config.hf_text_config.is_causal = False
            config.id2label = dict(enumerate(decision["codes"]))
            config.head_dtype = "float32"
            model_config.pooler_config.use_activation = False

    ModelRegistry.register_model(
        ARCHITECTURE, "karpo_vllm_model:KarpoForSequenceClassification"
    )
    MODELS_CONFIG_MAP[ARCHITECTURE] = KarpoConfig
