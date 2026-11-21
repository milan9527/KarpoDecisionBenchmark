set -Eeuo pipefail
mkdir -p /opt/karpo/rtx6-bootstrap
cat > /opt/karpo/rtx6-bootstrap/peer4.sh <<'RTX6_SCRIPT'
set -Eeuo pipefail
RTX6_SSH=(-i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${RTX6_SSH[@]}" ubuntu@18.212.111.93 true; then ready=true; break; fi
 sleep 5
done
$ready
ssh "${RTX6_SSH[@]}" ubuntu@18.212.111.93 'sudo /bin/bash -se' <<'RTX6_GUARD'
shutdown -c || true
systemctl list-unit-files --no-legend | while read -r unit rest; do
 if [[ "$unit" == karpo*.timer ]]; then systemctl disable --now "$unit"; fi
done
mkdir -p /opt/karpo/venv-vllm034 /opt/karpo/models/karpo-4b /opt/karpo/rtx6-bootstrap
RTX6_GUARD
echo 'Copy current vLLM environment'
rsync -a --exclude=__pycache__ -e "ssh ${RTX6_SSH[*]}" --rsync-path='sudo rsync' /opt/karpo/venv-vllm034/ ubuntu@18.212.111.93:/opt/karpo/venv-vllm034/
echo 'Copy exact current model; warm destination files'
rsync -a --ignore-times --whole-file -e "ssh ${RTX6_SSH[*]}" --rsync-path='sudo rsync' /opt/karpo/models/karpo-4b/ ubuntu@18.212.111.93:/opt/karpo/models/karpo-4b/
ssh "${RTX6_SSH[@]}" ubuntu@18.212.111.93 'sudo /bin/bash -se' <<'RTX6_VALIDATE'
/opt/karpo/venv-vllm034/bin/python -m pip check > /opt/karpo/rtx6-bootstrap/pip-check.log
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import datetime as dt,hashlib,json,pathlib,subprocess
result={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'replica_id':4,'weights':{}}
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

RTX6_SCRIPT
systemd-run --unit=karpo-rtx6-bootstrap-peer4 --property=RuntimeMaxSec=3600 --property=StandardOutput=append:/opt/karpo/rtx6-bootstrap/peer4.log --property=StandardError=inherit /bin/bash /opt/karpo/rtx6-bootstrap/peer4.sh
