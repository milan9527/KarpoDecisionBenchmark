#!/bin/bash
set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-single-20261120/results/setup
cat > /opt/karpo/experiments/vllm-single-20261120/install-vllm.sh <<'INSTALL'
#!/bin/bash
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-single-20261120/results/setup
finish() {
 result=$?
 printf '%s\n' "$result" > "$OUT/install-exit-code.txt"
 df -h /opt/karpo > "$OUT/disk-after.txt"
 aws s3 sync "$OUT/" s3://karpo-benchmark-20261119-115003-632930644527/results/vllm-single-20261120/setup/ --region us-east-1 --no-progress || true
}
trap finish EXIT
exec >> "$OUT/install.log" 2>&1
test ! -e /opt/karpo/venv-vllm034
/opt/karpo/venv/bin/python -m venv /opt/karpo/venv-vllm034
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir --upgrade pip
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir 'vllm==0.34.0' 'transformers==5.21.0' 'flash-linear-attention==0.5.2' 'karpo-decision[qwen] @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de' boto3 httpx
/opt/karpo/venv-vllm034/bin/python -m pip freeze > "$OUT/vllm-freeze.txt"
/opt/karpo/venv/bin/python -m pip freeze > "$OUT/native-freeze.txt"
/opt/karpo/venv-vllm034/bin/python -m pip check > "$OUT/pip-check.txt"
INSTALL
systemd-run --unit=karpo-vllm-install --property=RuntimeMaxSec=1800 /bin/bash /opt/karpo/experiments/vllm-single-20261120/install-vllm.sh
