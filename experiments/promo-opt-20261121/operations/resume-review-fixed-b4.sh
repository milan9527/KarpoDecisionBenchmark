set -euo pipefail
cd /opt/karpo/promo-opt-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import pathlib,json
p=pathlib.Path('experiments/promo-opt-20261121/results')
state=json.loads((p/'controllers/review.json').read_text())
assert state['status']=='error' and not state['completed']
assert 'not loaded' in state['error']
(p/'controllers/review.json').rename(p/'setup-failures/review-transition-attempt1.json')
fleet=json.loads((p/'current-fleet.json').read_text())
assert fleet['name']=='original-b16-powers'
fleet['units']={}
fleet['note']='All prior units already stopped by user-requested cancellation; no active fleet to stop again.'
(p/'current-fleet.json').write_text(json.dumps(fleet,indent=2)+'\n')
PY
systemd-run --unit karpo-opt-review-r1 --property=Type=exec --property=RuntimeMaxSec=7200 --working-directory /opt/karpo/promo-opt-20261121 /opt/karpo/venv-vllm034/bin/python -u scripts/promo_opt_review_controller.py
