set -eu
systemctl is-active karpo-opt-review-r1 karpo-opt-calibrated || true
journalctl -u karpo-opt-review-r1 -u karpo-opt-calibrated -n 2 --no-pager -o cat
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,pathlib
p=pathlib.Path('/opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results')
for f in sorted(p.glob('controllers/*.json'))+sorted(p.glob('runs/*/status.json'),key=lambda f:f.stat().st_mtime)[-2:]+sorted(p.glob('nodes/*/status.json'),key=lambda f:f.stat().st_mtime)[-3:]:
 x=json.loads(f.read_text());print(str(f.relative_to(p)),json.dumps({k:v for k,v in x.items() if k not in ['stages']}))
 for s in x.get('stages',[]):
  r=s['result'];print(s['name'],{k:r[k] for k in ['attempts','valid','accuracy','steady_valid_rps','p50_ms','failures'] if k in r})
PY
