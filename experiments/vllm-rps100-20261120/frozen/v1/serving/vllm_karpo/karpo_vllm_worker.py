"""Named worker RPC for provenance, without serializing executable callables."""


class KarpoInspectionWorker:
    def inspect_karpo(self):
        from collections import Counter
        import hashlib
        import torch

        model = self.model_runner.model
        dtypes = Counter()
        quant_methods = Counter()
        attention = []
        for _, parameter in model.named_parameters():
            dtypes[str(parameter.dtype)] += parameter.numel()
        for name, module in model.named_modules():
            method = getattr(module, "quant_method", None)
            if method is not None:
                quant_methods[type(method).__name__] += 1
            # The top-level hybrid model declares "decoder" for scheduling;
            # validate the actual full-attention operators, not that wrapper.
            if name.endswith(".self_attn.attn") and hasattr(module, "attn_type"):
                attention.append({"module": name, "type": str(module.attn_type)})
        readout = model.score.weight.detach().float().cpu().contiguous()
        return {
            "parameter_elements_by_dtype": dict(dtypes),
            "quant_methods": dict(quant_methods),
            "attention": attention,
            "readout_dtype": str(model.score.weight.dtype),
            "readout_shape": list(readout.shape),
            "readout_sha256": hashlib.sha256(readout.numpy().tobytes()).hexdigest(),
            "allocated_gib": torch.cuda.memory_allocated() / 2**30,
            "reserved_gib": torch.cuda.memory_reserved() / 2**30,
        }
