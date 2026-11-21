set -euo pipefail
cd /opt/karpo/promo-rtx6-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import datetime as dt,json,pathlib,subprocess,sys,urllib.request
sys.path.insert(0,'scripts')
from promo_rtx6_runtime import synchronize
p=pathlib.Path('experiments/promo-rtx6-20261121')
assert json.loads((p/'results/controller.json').read_text())['status']=='complete'
queue=subprocess.run(['systemctl','is-active','karpo-opt-queue'],capture_output=True,text=True)
assert queue.stdout.strip() not in ('active','activating')
fleet=json.loads((p/'fleet-v1.json').read_text())
backends=[]
for replica,url in enumerate(fleet['urls']):
 with urllib.request.urlopen(url+'/healthz',timeout=10) as r:
  backend=json.load(r)
 assert backend['max_num_seqs']==4 and backend['graph_policy']=='powers' and backend['replica_id']==replica
 backends.append(backend)
result={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),
        'batch_search_unit':queue.stdout.strip(),'backends':backends,
        'note':'Read-only service checks; no EC2 lifecycle action'}
(p/'results/final-service-state.json').write_text(json.dumps(result,indent=2)+'\n')
synchronize()
print(json.dumps({'batch_search':queue.stdout.strip(),'healthy_replicas':len(backends),'batch':4}))
PY
