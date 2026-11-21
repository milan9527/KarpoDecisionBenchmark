set -eu
systemctl is-active karpo-rtx6-controller karpo-rtx6-bootstrap-peer4 karpo-rtx6-bootstrap-peer5 || true
journalctl -u karpo-rtx6-controller -n 3 --no-pager -o cat
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,pathlib
p=pathlib.Path('/opt/karpo/promo-rtx6-20261121/experiments/promo-rtx6-20261121/results')
for f in [p/'controller.json',*sorted(p.glob('nodes/*/status.json')),*sorted(p.glob('runs/*/status.json'))]:
 if not f.exists():continue
 s=json.loads(f.read_text())
 print(str(f.relative_to(p)),{k:v for k,v in s.items() if k not in ['stages']})
 for step in s.get('stages',[]):
  r=step['result'];print(step['name'],{k:r[k] for k in ['attempts','correct','steady_valid_rps','p50_ms','failures'] if k in r})
PY
