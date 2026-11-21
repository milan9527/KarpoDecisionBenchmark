set -euo pipefail
cd /opt/karpo/promo-opt-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,time,pathlib
p=pathlib.Path('experiments/promo-opt-20261121/results/controllers/review.json')
for _ in range(720):
 s=json.loads(p.read_text())
 if s['status']=='complete':break
 if s['status']=='error':raise RuntimeError(s['error'])
 time.sleep(5)
else:raise RuntimeError('Timed out waiting for fixed-batch review baseline')
fleet=json.loads(pathlib.Path('experiments/promo-opt-20261121/results/current-fleet.json').read_text())
assert fleet['batch']==4 and fleet['graph']=='powers' and fleet['dataset']=='review-baseline'
PY
/opt/karpo/venv-vllm034/bin/python -u scripts/promo_opt_calibrated_runtime.py run --dataset review-baseline --name review-calibrated-confirmed --batch 4 --graph powers --protocol formal --urls http://127.0.0.1:18500 http://127.0.0.1:18501 http://172.31.42.209:18500 http://172.31.37.239:18500
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import sys
sys.path.insert(0,'scripts')
from promo_opt_controller import collect
collect()
PY
