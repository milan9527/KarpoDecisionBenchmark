set -u
systemctl show karpo-rps-install-fleet1 karpo-rps-fleet1-node-v1 --property=Id,ActiveState,SubState,Result
python3 - <<'CHECK'
from pathlib import Path
base=Path('/opt/karpo/experiments/vllm-rps100-20261120/results')
for suffix in ('setup/fleet1-install-exit.txt','setup/fleet1-prefetch.json','runs/fleet1-fleetnode-v1/status.json'):
 p=base/suffix
 if p.exists(): print(suffix,p.read_text()[:1200])
for suffix in ('setup/fleet1-install.log','setup/fleet1-node-launch.log','runs/fleet1-fleetnode-v1/b4/server-3/server.log'):
 p=base/suffix
 if p.exists(): print(suffix,'\n'.join(p.read_text(errors='replace').splitlines()[-6:]))
CHECK
nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv
