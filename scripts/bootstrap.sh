#!/bin/bash
set -Eeuo pipefail
exec >>/var/log/karpo-bootstrap.log 2>&1
date -u
mkdir -p /opt/karpo/{results,models,fixtures,scripts}
# Keep weights on EBS so future stop/start cycles preserve them. The completed
# November 19 run used instance NVMe; its root snapshot does not contain weights.
# Avoid the Xet path that stalled during the initial bootstrap.
export HF_HUB_DISABLE_XET=1
# A maximum unattended lifetime, not a planned test duration.
systemd-run --unit=karpo-safety-stop --on-active=120m /sbin/shutdown -h now
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv gcc libc6-dev
python3 -m venv /opt/karpo/venv
/opt/karpo/venv/bin/pip install --quiet uv
/opt/karpo/venv/bin/uv pip install --python /opt/karpo/venv/bin/python \
    --extra-index-url https://download.pytorch.org/whl/cu130 \
    'karpo-decision @ git+https://github.com/machinepulse-ai/karpo-decision@__CODE_SHA__' \
    'torch==2.14.1+cu130' 'torchvision==0.29.1+cu130' \
    'transformers==5.21.0' 'triton==3.8.0' 'accelerate==1.15.0' \
    boto3 httpx huggingface_hub psutil
/opt/karpo/venv/bin/python - <<'PY'
import json, pathlib, subprocess, platform
import torch, transformers, boto3
from huggingface_hub import snapshot_download
root = pathlib.Path("/opt/karpo")
environment = {
    "python": platform.python_version(), "torch": torch.__version__,
    "transformers": transformers.__version__, "cuda": torch.version.cuda,
    "gpu": subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                    "--format=csv,noheader"], text=True).strip(),
    "code_revision": "__CODE_SHA__", "model_revision": "__MODEL_SHA__",
    "model": "MachinePulseAI/karpo-decision-31B",
    "dependency_policy": "Core runtime pinned to upstream requirements-serve.txt",
}
(root / "results/environment.json").write_text(json.dumps(environment, indent=2))
subprocess.run([str(root / "venv/bin/pip"), "freeze"], stdout=(root / "results/pip-freeze.txt").open("w"), check=True)
print(json.dumps(environment), flush=True)
path = snapshot_download("MachinePulseAI/karpo-decision-31B", revision="__MODEL_SHA__",
                         local_dir=str(root / "models/karpo-31b"),
                         ignore_patterns=["assets/*"], max_workers=8)
print("MODEL_DOWNLOADED", path, flush=True)
(root / "bootstrap-ready").write_text(path)
PY
date -u
