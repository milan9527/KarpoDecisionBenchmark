set -Eeuo pipefail
cat /opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/nodes/dual-v1/status.json
nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv
/opt/karpo/venv-vllm034/bin/python /opt/karpo/promo-validation-v1/scripts/promo_rtx4_suite.py sync
