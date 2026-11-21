set -Eeuo pipefail
PROMO_ROOT=/opt/karpo/promo-validation-v1
PROMO_PY=/opt/karpo/venv-vllm034/bin/python
"$PROMO_PY" - <<'PROMO_COMPLETE'
import json,pathlib
path=pathlib.Path('/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/runs/rtx4-fp8-v1/status.json')
assert json.loads(path.read_text())['status']=='complete'
PROMO_COMPLETE
systemctl stop karpo-promo-backup-v1
PROMO_SSH=(-i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts)
for peer in 172.31.42.209 172.31.37.239; do
 rsync -a -e "ssh ${PROMO_SSH[*]}" --rsync-path='sudo rsync' ubuntu@$peer:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/ "$PROMO_ROOT/experiments/promo-risk-rtx4-20261121/results/"
done
"$PROMO_PY" "$PROMO_ROOT/scripts/promo_rtx4_suite.py" sync
systemctl is-active karpo-promo-dual-v1
for replica in 2 3; do
 if [ "$replica" = 2 ]; then peer=172.31.42.209; else peer=172.31.37.239; fi
 ssh "${PROMO_SSH[@]}" ubuntu@$peer "sudo systemctl is-active karpo-promo-peer$replica-v1"
done
