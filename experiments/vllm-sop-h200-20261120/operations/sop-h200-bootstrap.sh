set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup
cat > /opt/karpo/experiments/vllm-sop-h200-20261120/bootstrap-h200.sh <<'H200_BOOTSTRAP'
set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${SSH_OPTIONS[@]}" ubuntu@172.31.14.72 true; then ready=true; break; fi
 sleep 5
done
$ready
ssh "${SSH_OPTIONS[@]}" ubuntu@172.31.14.72 'sudo /bin/bash -s' <<'H200_INITIAL'
set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv > /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup/h200-hardware.csv
nvidia-smi topo -m > /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup/h200-topology.txt
test ! -e /run/systemd/shutdown/scheduled
set -Eeuo pipefail
mkdir -p /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup
cat > /opt/karpo/experiments/vllm-sop-h200-20261120/install-h200.sh <<'INSTALL'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-sop-h200-20261120/results/setup
finish() {
 result=$?
 printf '%s\n' "$result" > "$OUT/h200-install-exit.txt"
}
trap finish EXIT
exec >> "$OUT/h200-install.log" 2>&1
if [ ! -e /opt/karpo/venv-vllm034/bin/python ]; then
 /opt/karpo/venv/bin/python -m venv /opt/karpo/venv-vllm034
fi
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir --upgrade pip
/opt/karpo/venv-vllm034/bin/python -m pip install --no-cache-dir 'vllm==0.34.0' 'transformers==5.21.0' 'flash-linear-attention==0.5.2' 'karpo-decision[qwen] @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de' boto3 httpx
/opt/karpo/venv-vllm034/bin/python -m pip freeze > "$OUT/h200-vllm-freeze.txt"
/opt/karpo/venv-vllm034/bin/python -m pip check > "$OUT/h200-pip-check.txt"
INSTALL
systemd-run --unit=karpo-rps-install-h200 --property=RuntimeMaxSec=1800 /bin/bash /opt/karpo/experiments/vllm-sop-h200-20261120/install-h200.sh

set -Eeuo pipefail
cat > /opt/karpo/experiments/vllm-sop-h200-20261120/prefetch-h200.py <<'PY'
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
out=Path('/opt/karpo/experiments/vllm-sop-h200-20261120/results/setup/h200-prefetch.json')
out.write_text(json.dumps({'bytes':total,'seconds':time.monotonic()-started,'purpose':'Read-only page-cache warming for cold AMI snapshot; not an inference latency measurement'},indent=2)+'\n')
PY
systemd-run --unit=karpo-rps-h200-prefetch --property=RuntimeMaxSec=900 /usr/bin/python3 /opt/karpo/experiments/vllm-sop-h200-20261120/prefetch-h200.py

H200_INITIAL
cat > /opt/karpo/experiments/vllm-sop-h200-20261120/backup-h200.sh <<'BACKUP'
set -u
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
OUT=/opt/karpo/experiments/vllm-sop-h200-20261120/results
mkdir -p "$OUT/control-mirror"
while true; do
 rsync -a -e "ssh ${SSH_OPTIONS[*]}" --rsync-path='sudo rsync' --include='/setup/' --include='/setup/***' --include='/runs/' --include='/runs/***' --exclude='*' ubuntu@172.31.14.72:/opt/karpo/experiments/vllm-sop-h200-20261120/results/ "$OUT/control-mirror/"
 /opt/karpo/venv-vllm034/bin/python - <<'UPLOAD'
import boto3,json
from pathlib import Path
base=Path('/opt/karpo/experiments/vllm-sop-h200-20261120/results/control-mirror')
cache=base/'uploaded.json'
previous=json.loads(cache.read_text()) if cache.exists() else {}
s3=boto3.client('s3',region_name='us-east-1')
for folder in ('setup','runs'):
 for path in (base/folder).rglob('*'):
  if not path.is_file(): continue
  key=str(path.relative_to(base)); stat=path.stat(); sig=[stat.st_size,stat.st_mtime_ns]
  if previous.get(key)==sig: continue
  s3.upload_file(str(path),'karpo-benchmark-20261119-115003-632930644527','results/vllm-sop-h200-20261120/'+key)
  previous[key]=sig
cache.write_text(json.dumps(previous))
UPLOAD
 sleep 20
done
BACKUP
systemd-run --unit=karpo-sop-h200-backup --property=RuntimeMaxSec=14400 /bin/bash /opt/karpo/experiments/vllm-sop-h200-20261120/backup-h200.sh

H200_BOOTSTRAP
systemd-run --unit=karpo-sop-h200-bootstrap-v1 --property=RuntimeMaxSec=1200 --property=StandardOutput=append:/opt/karpo/experiments/vllm-sop-h200-20261120/results/setup/control-bootstrap.log --property=StandardError=inherit /bin/bash /opt/karpo/experiments/vllm-sop-h200-20261120/bootstrap-h200.sh
