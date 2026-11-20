set -Eeuo pipefail
cat > /opt/karpo/experiments/vllm-rps100-20261120/bootstrap-fleet1.sh <<'PEER_CONTROL'

set -Eeuo pipefail
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${SSH_OPTIONS[@]}" ubuntu@100.59.41.29 true; then ready=true; break; fi
 sleep 5
done
$ready
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/vllm-rps-fleet-v1.tar.gz /tmp/vllm-rps-fleet-v1.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
scp "${SSH_OPTIONS[@]}" /tmp/vllm-rps-fleet-v1.tar.gz ubuntu@100.59.41.29:/tmp/
ssh "${SSH_OPTIONS[@]}" ubuntu@100.59.41.29 'sudo bash -s' <<'PEER_INITIALIZE'
set -Eeuo pipefail
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
tar -xzf /tmp/vllm-rps-fleet-v1.tar.gz -C /opt/karpo
set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-rps100-20261120/results/setup
cat > /opt/karpo/experiments/vllm-rps100-20261120/install-fleet1.sh <<'INSTALL'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results/setup
finish() {
 result=$?
 printf '%s\n' "$result" > "$OUT/fleet1-install-exit.txt"
}
trap finish EXIT
exec >> "$OUT/fleet1-install.log" 2>&1
if [ ! -e /opt/karpo/venv-vllm034/bin/python ]; then
 /opt/karpo/venv/bin/python -m venv /opt/karpo/venv-vllm034
fi
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir --upgrade pip
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir 'vllm==0.34.0' 'transformers==5.21.0' 'flash-linear-attention==0.5.2' 'karpo-decision[qwen] @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de' boto3 httpx
/opt/karpo/venv-vllm034/bin/python -m pip freeze > "$OUT/fleet1-vllm-freeze.txt"
/opt/karpo/venv-vllm034/bin/python -m pip check > "$OUT/fleet1-pip-check.txt"
INSTALL
systemd-run --unit=karpo-rps-install-fleet1 --property=RuntimeMaxSec=1800 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/install-fleet1.sh

set -Eeuo pipefail
cat > /opt/karpo/experiments/vllm-rps100-20261120/prefetch-fleet1.py <<'PY'
import concurrent.futures,json,time
from pathlib import Path
root=Path('/opt/karpo/models/karpo-4b')
started=time.monotonic()
chunk=32*1024*1024
jobs=[(p,offset,min(chunk,p.stat().st_size-offset)) for p in root.glob('*.safetensors') for offset in range(0,p.stat().st_size,chunk)]
def read(job):
 p,offset,size=job
 with p.open('rb') as f:
  f.seek(offset)
  data=f.read(size)
  assert len(data)==size
 return len(data)
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
 total=sum(pool.map(read,jobs))
out=Path('/opt/karpo/experiments/vllm-rps100-20261120/results/setup/fleet1-prefetch.json')
out.write_text(json.dumps({'bytes':total,'seconds':time.monotonic()-started,'purpose':'Read-only page-cache warming for cold AMI snapshot; not an inference latency measurement'},indent=2)+'\n')
PY
systemd-run --unit=karpo-rps-fleet1-prefetch --property=RuntimeMaxSec=900 /usr/bin/python3 /opt/karpo/experiments/vllm-rps100-20261120/prefetch-fleet1.py


cat > /opt/karpo/experiments/vllm-rps100-20261120/launch-fleet1-node.sh <<'PEER_NODE'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results/setup
exec >> "$OUT/fleet1-node-launch.log" 2>&1
for attempt in $(seq 1 360); do
 if [ -f "$OUT/fleet1-install-exit.txt" ]; then break; fi
 sleep 5
done
test "$(cat "$OUT/fleet1-install-exit.txt")" = 0
/opt/karpo/venv-vllm034/bin/python -m pip install --no-deps /opt/karpo/serving/vllm_karpo
/opt/karpo/venv-vllm034/bin/python /opt/karpo/tests/test_vllm_rps.py > "$OUT/fleet1-harness-tests.log" 2>&1
test ! -e /run/systemd/shutdown/scheduled
systemctl list-timers --all --no-pager > "$OUT/fleet1-timers.txt"
nvidia-smi > "$OUT/fleet1-nvidia-smi.txt"
exec /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_fleet.py --mode node --name fleet1-fleetnode-v1 --replica-ids 3 --batch 4 --roleless
PEER_NODE
systemd-run --unit=karpo-rps-fleet1-node-v1 --property=RuntimeMaxSec=21600 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/launch-fleet1-node.sh

PEER_INITIALIZE
cat > /opt/karpo/experiments/vllm-rps100-20261120/backup-fleet1.sh <<'BACKUP'
set -u
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results
mkdir -p "$OUT/fleet1"
while true; do
 rsync -a -e "ssh ${SSH_OPTIONS[*]}" --rsync-path='sudo rsync' --include='/setup/' --include='/setup/fleet1-*' --include='/runs/' --include='/runs/fleet1-*/***' --exclude='*' ubuntu@100.59.41.29:/opt/karpo/experiments/vllm-rps100-20261120/results/ "$OUT/fleet1/"
 /opt/karpo/venv-vllm034/bin/python - <<'UPLOAD'
import boto3,json
from pathlib import Path
base=Path('/opt/karpo/experiments/vllm-rps100-20261120/results/fleet1')
cache=base/'uploaded.json'
previous=json.loads(cache.read_text()) if cache.exists() else {}
s3=boto3.client('s3',region_name='us-east-1')
for folder in ('setup','runs'):
 for path in (base/folder).rglob('*'):
  if not path.is_file(): continue
  key=str(path.relative_to(base)); stat=path.stat(); sig=[stat.st_size,stat.st_mtime_ns]
  if previous.get(key)==sig: continue
  s3.upload_file(str(path),'karpo-benchmark-20261119-115003-632930644527','results/vllm-rps100-20261120/'+key)
  previous[key]=sig
cache.write_text(json.dumps(previous))
UPLOAD
 sleep 20
done
BACKUP
systemd-run --unit=karpo-rps-fleet1-backup --property=RuntimeMaxSec=14400 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/backup-fleet1.sh

PEER_CONTROL
systemd-run --unit=karpo-rps-bootstrap-fleet1 --property=RuntimeMaxSec=1200 --property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/fleet1-control.log --property=StandardError=inherit /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/bootstrap-fleet1.sh
