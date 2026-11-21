set -eu
/opt/karpo/venv-vllm034/bin/python - <<'PY'
from pathlib import Path
p=Path('/opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results/nodes/original-b8-powers-peer3')
f=p/'replica-3/server.log';s=f.read_text(errors='replace').splitlines()
for i,line in enumerate(s):
 if any(x in line for x in ['Traceback','OutOfMemory','out of memory','Error:', 'terminate','killed','Killed']):
  print('\n'.join(s[max(0,i-4):min(len(s),i+14)]))
print('node', (p/'status.json').read_text())
PY
free -m
journalctl -k --since '2026-11-21 03:31:00' --no-pager -o cat | tail -12
