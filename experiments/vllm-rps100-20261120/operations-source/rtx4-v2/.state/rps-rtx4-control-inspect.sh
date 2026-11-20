set -u
systemctl show karpo-rps-bootstrap-fleet1 karpo-rps-rtx4-tunnel-v1 --property=Id,ActiveState,SubState,Result
curl --max-time 10 -sS https://checkip.amazonaws.com
python3 - <<'CHECK'
from pathlib import Path
base=Path('/opt/karpo/experiments/vllm-rps100-20261120/results/setup')
for name in ('fleet1-control.log','rtx4-tunnel.log'):
 p=base/name
 if p.exists():print(name,'\n'.join(p.read_text(errors='replace').splitlines()[-10:]))
CHECK
