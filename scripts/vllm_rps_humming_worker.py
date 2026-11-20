"""Record the actual Humming operand types; reject implicit A16 fallback."""
from karpo_vllm_worker import KarpoInspectionWorker


class HummingInspectionWorker(KarpoInspectionWorker):
    def inspect_humming(self):
        rows = []
        for name, module in self.model_runner.model.named_modules():
            method = getattr(module, "quant_method", None)
            if method is None:
                continue
            pending, seen = [method], set()
            while pending:
                obj = pending.pop()
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                if type(obj).__name__ == "HummingMxFp4LinearKernel":
                    config = obj.layer_config
                    rows.append({"module": name, "kernel": type(obj).__name__,
                                 "a_dtype": str(config.a_dtype), "b_dtype": str(config.b_dtype),
                                 "a_bits": config.a_dtype.num_bits, "b_bits": config.b_dtype.num_bits,
                                 "layer_config": str(config)})
                for value in vars(obj).values():
                    if hasattr(value, "__dict__") and (
                        "Method" in type(value).__name__ or "Kernel" in type(value).__name__
                    ):
                        pending.append(value)
        return rows
