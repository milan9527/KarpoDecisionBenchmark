set -euo pipefail
systemctl stop karpo-opt-queue
cd /opt/karpo/promo-opt-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,datetime,pathlib,sys
sys.path.insert(0,'scripts')
from promo_opt_controller import parallel,collect
from promo_opt_runtime import dump
p=pathlib.Path('experiments/promo-opt-20261121/results')
fleet=json.loads((p/'current-fleet.json').read_text())
parallel({host:'systemctl stop '+unit+'\n' for host,unit in fleet['units'].items()})
state=json.loads((p/'controllers/performance.json').read_text())
record={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'reason':'User: batch不要调了; stop batch tuning and continue review optimization at batch 4 / powers', 'previous_controller_state':state,'stopped_fleet':fleet}
dump(p/'batch-tuning-interruption.json',record)
state.update(status='cancelled_by_user',ended_utc=record['utc'],reason=record['reason'])
dump(p/'controllers/performance.json',state)
for f in (p/'runs').glob('*/status.json'):
 x=json.loads(f.read_text())
 if x['status']=='running':
  dump(f.parent/'interrupted-state.json',x)
  x.update(status='cancelled_by_user',ended_utc=record['utc'],reason=record['reason']);dump(f,x)
selection={'selected':{'name':'original-b4-powers','batch':4,'graph':'powers'},'candidates':[], 'any_probe_passed':False,'selection_basis':'User stopped batch tuning; revert to original stable batch 4 / powers. No further batch search.','frozen_utc':record['utc']}
for f in sorted((p/'runs').glob('original*/status.json')):
 x=json.loads(f.read_text())
 if x['status']=='complete':
  probe=next((s['result'] for s in x['stages'] if s['name']=='probe'),None)
  if probe:selection['candidates'].append({'name':f.parent.name,'batch':x['batch'],'graph':x['graph'],'probe':probe})
dump(p/'performance-selection.json',selection)
collect()
PY
systemd-run --unit karpo-opt-review --property=Type=exec --property=RuntimeMaxSec=7200 --working-directory /opt/karpo/promo-opt-20261121 /opt/karpo/venv-vllm034/bin/python -u scripts/promo_opt_review_controller.py
