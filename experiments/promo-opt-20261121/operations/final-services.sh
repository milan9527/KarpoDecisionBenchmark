set -euo pipefail
cd /opt/karpo/promo-opt-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import datetime as dt,json,pathlib,subprocess,sys,urllib.request
sys.path.insert(0,'scripts')
from promo_opt_runtime import synchronize
p=pathlib.Path('experiments/promo-opt-20261121/results')
assert json.loads((p/'runs/review-calibrated-confirmed/status.json').read_text())['status']=='complete'
queue=subprocess.run(['systemctl','is-active','karpo-opt-queue'],capture_output=True,text=True)
assert queue.stdout.strip() not in ('active','activating')
urls=['http://127.0.0.1:18500','http://127.0.0.1:18501',
      'http://172.31.42.209:18500','http://172.31.37.239:18500']
backends=[]
for url in urls:
 with urllib.request.urlopen(url+'/healthz',timeout=10) as r:
  backend=json.load(r)
 assert backend['max_num_seqs']==4 and backend['graph_policy']=='powers'
 backends.append(backend)
result={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),
        'batch_search_unit':queue.stdout.strip(),'backends':backends,
        'note':'Read-only service checks; no EC2 lifecycle action'}
(p/'final-service-state.json').write_text(json.dumps(result,indent=2)+'\n')
synchronize()
print(json.dumps({'batch_search':queue.stdout.strip(),'healthy_replicas':len(backends),'batch':4}))
PY
