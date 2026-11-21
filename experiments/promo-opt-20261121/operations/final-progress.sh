set -eu
systemctl is-active karpo-opt-review-r1 karpo-opt-calibrated || true
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,pathlib
p=pathlib.Path('/opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results')
for name in ['review-confirmed','review-calibrated-confirmed']:
 f=p/'runs'/name/'status.json'
 if not f.exists():
  print(name,'not started')
  continue
 s=json.loads(f.read_text())
 print(name,s['status'],s.get('current'),s.get('error'))
 for row in s['stages']:
  r=row['result']
  print(row['name'],{k:r[k] for k in ['attempts','correct','steady_valid_rps','p50_ms','failures'] if k in r})
PY
