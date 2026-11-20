set -Eeuo pipefail
H100_ADDRESS=172.31.25.147
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/vllm-rps-h100-v1.tar.gz /tmp/vllm-rps-h100-v1.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 9993c2924c84a06d5375689f40a4f36e7be622ee77acbc30dff4a003196b43ce /tmp/vllm-rps-h100-v1.tar.gz | sha256sum -c -
scp "${SSH_OPTIONS[@]}" /tmp/vllm-rps-h100-v1.tar.gz "ubuntu@$H100_ADDRESS:/tmp/"
ssh "${SSH_OPTIONS[@]}" "ubuntu@$H100_ADDRESS" 'sudo bash -s' <<'REMOTE'
set -Eeuo pipefail
printf '%s  %s\n' 9993c2924c84a06d5375689f40a4f36e7be622ee77acbc30dff4a003196b43ce /tmp/vllm-rps-h100-v1.tar.gz | sha256sum -c -
tar -xzf /tmp/vllm-rps-h100-v1.tar.gz -C /opt/karpo
cat > /opt/karpo/experiments/vllm-rps100-20261120/launch-h100-v1.sh <<'RUN'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results/setup
mkdir -p "$OUT"
exec >> "$OUT/h100-launch.log" 2>&1
for attempt in $(seq 1 360); do
 if [ -f "$OUT/h100-install-exit.txt" ]; then break; fi
 sleep 5
done
test "$(cat "$OUT/h100-install-exit.txt")" = 0
/opt/karpo/venv-vllm034/bin/python -m pip install --no-deps /opt/karpo/serving/vllm_karpo
/opt/karpo/venv-vllm034/bin/python /opt/karpo/tests/test_vllm_rps.py > "$OUT/h100-harness-tests.log" 2>&1
nvidia-smi > "$OUT/h100-nvidia-smi.txt"
test ! -e /run/systemd/shutdown/scheduled
systemctl list-timers --all --no-pager > "$OUT/h100-timers.txt"
/opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_h100_suite.py --name h100-fp8-v1 --gpus 1 --replicas 1 --batches 1 4 --seconds 20
RUN
systemd-run --unit=karpo-rps-h100-v1 --property=RuntimeMaxSec=5400 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/launch-h100-v1.sh
REMOTE
cat > /opt/karpo/experiments/vllm-rps100-20261120/backup-h100.sh <<'BACKUP'
set -u
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results
mkdir -p "$OUT/h100"
while true; do
 rsync -a -e "ssh ${SSH_OPTIONS[*]}" --rsync-path='sudo rsync' --include='/setup/' --include='/setup/h100-*' --include='/runs/' --include='/runs/h100-*/***' --exclude='*' ubuntu@172.31.25.147:/opt/karpo/experiments/vllm-rps100-20261120/results/ "$OUT/h100/"
 /opt/karpo/venv-vllm034/bin/python - <<'UPLOAD'
import boto3,json
from pathlib import Path
base=Path('/opt/karpo/experiments/vllm-rps100-20261120/results/h100')
cache=base/'uploaded.json'
previous=json.loads(cache.read_text()) if cache.exists() else {}
s3=boto3.client('s3',region_name='us-east-1')
for folder in ('setup','runs'):
 for path in (base/folder).rglob('*'):
  if not path.is_file(): continue
  key=str(path.relative_to(base)); stat=path.stat(); sig=[stat.st_size,stat.st_mtime_ns]
  if previous.get(key)==sig: continue
  s3.upload_file(str(path),'karpo-benchmark-20261119-115003-632930644527','results/vllm-rps100-20261120/'+key)
  previous[key]=sig
cache.write_text(json.dumps(previous))
UPLOAD
 sleep 20
done
BACKUP
systemd-run --unit=karpo-rps-h100-backup --property=RuntimeMaxSec=14400 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/backup-h100.sh
