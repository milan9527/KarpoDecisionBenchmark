set -Eeuo pipefail
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import httpx
urls=['http://127.0.0.1:18500','http://172.31.40.12:18500','http://172.31.40.12:18501','http://172.31.25.147:18500']
for i,url in enumerate(urls):
 r=httpx.get(url+'/healthz',timeout=10,trust_env=False)
 r.raise_for_status()
 assert r.json()['replica_id']==i,(url,r.text)
 assert r.json()['settings']['quantization']=='fp8_per_channel'
print('All four private replicas ready')
PY
systemd-run --unit=karpo-rps-mixed4-v1 --property=RuntimeMaxSec=3600 --property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/mixed4-launch.log --property=StandardError=inherit /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_fleet.py --mode driver --name mixed4-fp8-v1 --urls http://127.0.0.1:18500 http://172.31.40.12:18500 http://172.31.40.12:18501 http://172.31.25.147:18500 --batch 4 --confirm
