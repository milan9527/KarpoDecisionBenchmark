set -Eeuo pipefail
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/sop-h200-v2.tar.gz /tmp/sop-h200-v2.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' aec72443cf18e777fb45ea449d36ea9986519ae47b8e43eeee13a414a823174f /tmp/sop-h200-v2.tar.gz | sha256sum -c -
scp "${SSH_OPTIONS[@]}" /tmp/sop-h200-v2.tar.gz ubuntu@172.31.14.72:/tmp/
ssh "${SSH_OPTIONS[@]}" ubuntu@172.31.14.72 'sudo /bin/bash -s' <<'H200_DEPLOY'
set -Eeuo pipefail
printf '%s  %s\n' aec72443cf18e777fb45ea449d36ea9986519ae47b8e43eeee13a414a823174f /tmp/sop-h200-v2.tar.gz | sha256sum -c -
tar -xzf /tmp/sop-h200-v2.tar.gz -C /opt/karpo
cat > /opt/karpo/experiments/vllm-sop-h200-20261120/launch-v2.sh <<'H200_RUN'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-sop-h200-20261120/results/setup
export CUDA_HOME=/usr/local/cuda-13.0
export PATH=/opt/karpo/venv-vllm034/bin:/usr/local/cuda-13.0/bin:$PATH
exec >> "$OUT/h200-suite-v2-launch.log" 2>&1
finish() { result=$?; printf '%s\n' "$result" > "$OUT/h200-suite-v2-exit.txt"; }
trap finish EXIT
for attempt in $(seq 1 360); do
 if test -f "$OUT/h200-install-exit.txt" && test -f "$OUT/h200-prefetch.json"; then break; fi
 sleep 5
done
test "$(cat "$OUT/h200-install-exit.txt")" = 0
test -f "$OUT/h200-prefetch.json"
/opt/karpo/venv-vllm034/bin/python -m pip install --no-deps /opt/karpo/serving/vllm_karpo
/opt/karpo/venv-vllm034/bin/python -m pip check > "$OUT/h200-pip-check-final-v2.txt"
/opt/karpo/venv-vllm034/bin/python /opt/karpo/tests/test_vllm_rps.py > "$OUT/h200-harness-tests-v2.log" 2>&1
/opt/karpo/venv-vllm034/bin/python /opt/karpo/tests/test_vllm_karpo.py > "$OUT/h200-adapter-tests-v2.log" 2>&1
/opt/karpo/venv-vllm034/bin/python - <<'CUDA_PROOF'
import json,torch
from pathlib import Path
assert torch.cuda.device_count()==8
rows=[]
for gpu in range(8):
 assert 'H200' in torch.cuda.get_device_name(gpu)
 x=torch.ones((32,32),device=f'cuda:{gpu}')
 y=x@x
 torch.cuda.synchronize(gpu)
 assert y[0,0].item()==32
 rows.append({'gpu':gpu,'name':torch.cuda.get_device_name(gpu),'compute_capability':torch.cuda.get_device_capability(gpu),'cuda_matmul_passed':True})
Path('/opt/karpo/experiments/vllm-sop-h200-20261120/results/setup/h200-cuda-proof-v2.json').write_text(json.dumps(rows,indent=2)+'\n')
CUDA_PROOF
test ! -e /run/systemd/shutdown/scheduled
systemctl list-timers --all --no-pager > "$OUT/h200-timers.txt"
/opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/sop_h200_suite.py --name h200-fp8-v2 --revision v2 --gpus 8 --replicas 1 2 4 8 --batches 1 4
H200_RUN
systemd-run --unit=karpo-sop-h200-v2 --property=RuntimeMaxSec=7200 --property=TimeoutStopSec=300 /bin/bash /opt/karpo/experiments/vllm-sop-h200-20261120/launch-v2.sh
H200_DEPLOY
