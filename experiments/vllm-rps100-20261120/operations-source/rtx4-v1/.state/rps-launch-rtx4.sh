set -Eeuo pipefail
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import httpx
urls=['http://127.0.0.1:18500','http://172.31.40.12:18500','http://172.31.40.12:18501','http://127.0.0.1:18503']
settings=[]
for i,url in enumerate(urls):
 r=httpx.get(url+'/healthz',timeout=10,trust_env=False)
 r.raise_for_status()
 health=r.json()
 assert health['replica_id']==i,(url,r.text)
 assert health['settings']['quantization']=='fp8_per_channel'
 assert health['max_num_seqs']==4
 settings.append(health['settings'])
assert all(s==settings[0] for s in settings)
print('Four RTX replicas ready with identical inference settings; replica 3 uses SSH forwarding')
PY
systemd-run --unit=karpo-rps-rtx4-v1 --property=RuntimeMaxSec=3600 --property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/rtx4-launch.log --property=StandardError=inherit /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_fleet.py --mode driver --name rtx4-fp8-v1 --urls http://127.0.0.1:18500 http://172.31.40.12:18500 http://172.31.40.12:18501 http://127.0.0.1:18503 --batch 4 --confirm
