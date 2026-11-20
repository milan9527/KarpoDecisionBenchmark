"""Record software, model metadata and exact installed adapter provenance."""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess

import karpo_decision
import torch
import transformers

ROOT = Path(__file__).resolve().parents[1]
package = Path(karpo_decision.__file__).parent
model = ROOT / "models/karpo-4b"
result = {
    "torch": torch.__version__,
    "transformers": transformers.__version__,
    "cuda": torch.version.cuda,
    "gpu": subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
        text=True,
    ),
    "karpo_source": json.loads(importlib.metadata.distribution("karpo-decision").read_text("direct_url.json")),
    "karpo_source_sha256": {
        str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in package.rglob("*.py")
    },
    "model_metadata_sha256": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in model.glob("*.json")
    },
    "model_weights_bytes": sum(p.stat().st_size for p in model.glob("*.safetensors")),
}
try:
    import karpo_vllm
    result["vllm"] = importlib.metadata.version("vllm")
    directory = Path(karpo_vllm.__file__).parent
    result["adapter_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.glob("karpo_vllm*.py")
    }
except ImportError:
    pass
print(json.dumps(result, indent=2))
