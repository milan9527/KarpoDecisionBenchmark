"""Prepare only the two newly allocated nodes, copying the exact current runtime."""
import ipaddress
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "experiments/promo-rtx6-20261121/operations"
peers = json.loads((OPS / "new-peers.json").read_text())
for peer in peers:
    replica = peer["replica_id"]
    address = str(ipaddress.ip_address(peer["public_ip"]))
    body = r"""set -Eeuo pipefail
RTX6_SSH=(-i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${RTX6_SSH[@]}" ubuntu@__ADDRESS__ true; then ready=true; break; fi
 sleep 5
done
$ready
ssh "${RTX6_SSH[@]}" ubuntu@__ADDRESS__ 'sudo /bin/bash -se' <<'RTX6_GUARD'
shutdown -c || true
systemctl list-unit-files --no-legend | while read -r unit rest; do
 if [[ "$unit" == karpo*.timer ]]; then systemctl disable --now "$unit"; fi
done
mkdir -p /opt/karpo/venv-vllm034 /opt/karpo/models/karpo-4b /opt/karpo/rtx6-bootstrap
RTX6_GUARD
echo 'Copy current vLLM environment'
rsync -a --exclude=__pycache__ -e "ssh ${RTX6_SSH[*]}" --rsync-path='sudo rsync' /opt/karpo/venv-vllm034/ ubuntu@__ADDRESS__:/opt/karpo/venv-vllm034/
echo 'Copy exact current model; warm destination files'
rsync -a --ignore-times --whole-file -e "ssh ${RTX6_SSH[*]}" --rsync-path='sudo rsync' /opt/karpo/models/karpo-4b/ ubuntu@__ADDRESS__:/opt/karpo/models/karpo-4b/
ssh "${RTX6_SSH[@]}" ubuntu@__ADDRESS__ 'sudo /bin/bash -se' <<'RTX6_VALIDATE'
/opt/karpo/venv-vllm034/bin/python -m pip check > /opt/karpo/rtx6-bootstrap/pip-check.log
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import datetime as dt,hashlib,json,pathlib,subprocess
result={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'replica_id':__REPLICA__,'weights':{}}
for p in sorted(pathlib.Path('/opt/karpo/models/karpo-4b').glob('*.safetensors')):
 h=hashlib.sha256()
 with p.open('rb') as stream:
  for chunk in iter(lambda:stream.read(16*1024*1024),b''):h.update(chunk)
 result['weights'][p.name]={'sha256':h.hexdigest(),'bytes':p.stat().st_size}
result['hardware']=subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.total,driver_version','--format=csv'],text=True)
pathlib.Path('/opt/karpo/rtx6-bootstrap/ready.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
PY
test ! -e /run/systemd/shutdown/scheduled
RTX6_VALIDATE
echo 'Bootstrap complete'
"""
    body = body.replace("__ADDRESS__", address).replace("__REPLICA__", str(replica))
    script = (
        "set -Eeuo pipefail\nmkdir -p /opt/karpo/rtx6-bootstrap\n"
        f"cat > /opt/karpo/rtx6-bootstrap/peer{replica}.sh <<'RTX6_SCRIPT'\n{body}\nRTX6_SCRIPT\n"
        f"systemd-run --unit=karpo-rtx6-bootstrap-peer{replica} --property=RuntimeMaxSec=3600 "
        f"--property=StandardOutput=append:/opt/karpo/rtx6-bootstrap/peer{replica}.log "
        f"--property=StandardError=inherit /bin/bash /opt/karpo/rtx6-bootstrap/peer{replica}.sh\n")
    path = OPS / f"bootstrap-peer{replica}.sh"
    path.write_text(script)
    subprocess.run(["bash", "-n", str(path)], check=True)
    subprocess.run(["python", str(ROOT / "scripts/promo_rtx6_cloud.py"), "send", str(path),
                    "--host", "dual", "--seconds", "60"], check=True)
