set -Eeuo pipefail
cat > /opt/karpo/experiments/vllm-rps100-20261120/prefetch-h100.py <<'PY'
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
out=Path('/opt/karpo/experiments/vllm-rps100-20261120/results/setup/h100-prefetch.json')
out.write_text(json.dumps({'bytes':total,'seconds':time.monotonic()-started,'purpose':'Read-only page-cache warming for cold AMI snapshot; not an inference latency measurement'},indent=2)+'\n')
PY
systemd-run --unit=karpo-rps-h100-prefetch --property=RuntimeMaxSec=900 /opt/karpo/venv-vllm034/bin/python /opt/karpo/experiments/vllm-rps100-20261120/prefetch-h100.py
