#!/bin/bash
set -Eeuo pipefail
RESULT_BUCKET="${1:?results bucket required}"
PROJECT=/opt/karpo
QWEN_RESULTS="$PROJECT/experiments/qwen-retry-20261120/results"
REPLICA_RESULTS="$PROJECT/experiments/replicas-retry-20261120/results"
mkdir -p "$QWEN_RESULTS" "$REPLICA_RESULTS"
exec >>"$QWEN_RESULTS/launcher.log" 2>&1
finish() {
    aws s3 sync "$QWEN_RESULTS/" "s3://$RESULT_BUCKET/results/qwen-retry-20261120/" --region us-east-1 --no-progress || true
    aws s3 sync "$REPLICA_RESULTS/" "s3://$RESULT_BUCKET/results/replicas-retry-20261120/" --region us-east-1 --no-progress || true
    /sbin/shutdown -h now
}
trap finish EXIT
export HF_HUB_DISABLE_XET=1 PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
printf 'torch==2.14.1+cu130\ntransformers==5.21.0\n' > /tmp/karpo-core-constraints.txt
"$PROJECT/venv/bin/python" -m pip install \
    --constraint /tmp/karpo-core-constraints.txt \
    --report "$QWEN_RESULTS/dependency-install.json" \
    flash-linear-attention==0.5.2 fla-core==0.5.2
"$PROJECT/venv/bin/python" - <<'PY'
import importlib.metadata
import torch
import transformers
from karpo_decision.families.qwen.packed import _kernels
assert torch.__version__ == "2.14.1+cu130"
assert transformers.__version__ == "5.21.0"
assert importlib.metadata.version("flash-linear-attention") == "0.5.2"
assert importlib.metadata.version("fla-core") == "0.5.2"
assert len(_kernels()) == 3
print("Qwen kernels imported successfully; core runtime versions preserved", flush=True)
PY
"$PROJECT/venv/bin/python" -u "$PROJECT/scripts/qwen_retry_suite.py" --bucket "$RESULT_BUCKET"
"$PROJECT/venv/bin/python" -u "$PROJECT/scripts/replica_retry_suite.py" --bucket "$RESULT_BUCKET" \
    >>"$REPLICA_RESULTS/launcher.log" 2>&1
