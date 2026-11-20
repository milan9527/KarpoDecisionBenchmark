set -Eeuo pipefail
/opt/karpo/venv/bin/python - <<'PREFLIGHT'
import json
from pathlib import Path
p=Path('/opt/karpo/experiments/vllm-single-20261120/results/smoke.json')
assert json.loads(p.read_text())['status']=='passed', 'Compatibility gate has not passed'
PREFLIGHT
systemctl stop karpo-vllm-development-04.service
aws s3 cp /opt/karpo/experiments/vllm-single-20261120/results/development-04/ s3://karpo-benchmark-20261119-115003-632930644527/results/vllm-single-20261120/development-04/ --recursive --region us-east-1 --no-progress
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/vllm-single-20261120.tar.gz /tmp/karpo-vllm-single-inputs.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' '8c614c0409b6fd642eef332fa405c35c3a5773f0837f863bbd2e6c09f5e348d9' /tmp/karpo-vllm-single-inputs.tar.gz | sha256sum -c -
test ! -e /opt/karpo/experiments/vllm-single-20261120/manifest.json
tar -xzf /tmp/karpo-vllm-single-inputs.tar.gz -C /opt/karpo
/opt/karpo/venv-vllm034/bin/python -m pip install --no-deps /opt/karpo/serving/vllm_karpo
/opt/karpo/venv-vllm034/bin/python /opt/karpo/tests/test_vllm_karpo.py > /opt/karpo/experiments/vllm-single-20261120/results/setup/final-adapter-tests.log 2>&1
cat > /opt/karpo/experiments/vllm-single-20261120/launch-suite.sh <<'RUN'
#!/bin/bash
set -Eeuo pipefail
finish() {
 aws s3 cp /opt/karpo/experiments/vllm-single-20261120/results/ s3://karpo-benchmark-20261119-115003-632930644527/results/vllm-single-20261120/ --recursive --region us-east-1 --no-progress || true
 /sbin/shutdown -h now
}
trap finish EXIT
/opt/karpo/venv/bin/python -u /opt/karpo/scripts/vllm_single_suite.py >> /opt/karpo/experiments/vllm-single-20261120/results/launcher.log 2>&1
RUN
systemd-run --unit=karpo-vllm-single-suite --property=RuntimeMaxSec=5100 /bin/bash /opt/karpo/experiments/vllm-single-20261120/launch-suite.sh
