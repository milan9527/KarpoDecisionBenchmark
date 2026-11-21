set -euo pipefail
cd /opt/karpo/promo-opt-20261121
ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@172.31.37.239 'sudo /bin/bash -se' <<'PEER'
systemctl show karpo-opt-original-b8-powers-peer3 -p Restart -p RuntimeMaxUSec -p TimeoutStartUSec -p TimeoutStopUSec -p ActiveState
systemctl stop karpo-opt-original-b8-powers-peer3 || true
/opt/karpo/venv-vllm034/bin/python - <<'PY'
from pathlib import Path
import json,datetime
p=Path('/opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results')
target=p/'setup-failures/b8-peer3-attempt1';target.parent.mkdir(parents=True,exist_ok=True)
(p/'nodes/original-b8-powers-peer3').rename(target)
(target/'recovery.json').write_text(json.dumps({'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'reason':'Engine received SIGTERM during startup; no model measurements begun; retain failed attempt and retry identical settings under a new service unit'},indent=2)+'\n')
PY
systemd-run --unit karpo-opt-b8-recovery1-peer3 --property=Type=exec --property=TimeoutStopSec=90 --property=RuntimeMaxSec=21600 --working-directory /opt/karpo/promo-opt-20261121 /opt/karpo/venv-vllm034/bin/python scripts/promo_opt_runtime.py node --dataset original --name original-b8-powers-peer3 --batch 8 --graph powers --replica-ids 3 --bind 0.0.0.0
PEER
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,pathlib
p=pathlib.Path('experiments/promo-opt-20261121/results/current-fleet.json');x=json.loads(p.read_text())
assert x['name']=='original-b8-powers'
x['units']['peer3']='karpo-opt-b8-recovery1-peer3';p.write_text(json.dumps(x,indent=2)+'\n')
PY
rsync -a -e 'ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts' --rsync-path='sudo rsync' ubuntu@172.31.37.239:/opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results/setup-failures/ experiments/promo-opt-20261121/results/setup-failures/
/opt/karpo/venv-vllm034/bin/python scripts/promo_opt_runtime.py sync
