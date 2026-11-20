set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-rps100-20261120/results/setup
cat > /opt/karpo/experiments/vllm-rps100-20261120/install-h100.sh <<'INSTALL'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results/setup
finish() {
 result=$?
 printf '%s\n' "$result" > "$OUT/h100-install-exit.txt"
}
trap finish EXIT
exec >> "$OUT/h100-install.log" 2>&1
if [ ! -e /opt/karpo/venv-vllm034/bin/python ]; then
 /opt/karpo/venv/bin/python -m venv /opt/karpo/venv-vllm034
fi
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir --upgrade pip
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir 'vllm==0.34.0' 'transformers==5.21.0' 'flash-linear-attention==0.5.2' 'karpo-decision[qwen] @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de' boto3 httpx
/opt/karpo/venv-vllm034/bin/python -m pip freeze > "$OUT/h100-vllm-freeze.txt"
/opt/karpo/venv-vllm034/bin/python -m pip check > "$OUT/h100-pip-check.txt"
INSTALL
systemd-run --unit=karpo-rps-install-h100 --property=RuntimeMaxSec=1800 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/install-h100.sh
