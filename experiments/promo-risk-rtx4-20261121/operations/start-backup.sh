set -Eeuo pipefail
cat > /opt/karpo/promo-backup-v1.sh <<'PROMO_BACKUP'
set -u
PROMO_SSH=(-i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts)
while true; do
 rsync -a -e "ssh ${PROMO_SSH[*]}" --rsync-path='sudo rsync' ubuntu@172.31.42.209:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/ /opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/ || true
 rsync -a -e "ssh ${PROMO_SSH[*]}" --rsync-path='sudo rsync' ubuntu@172.31.37.239:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/ /opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/ || true
 /opt/karpo/venv-vllm034/bin/python /opt/karpo/promo-validation-v1/scripts/promo_rtx4_suite.py sync || true
 sleep 30
done

PROMO_BACKUP
systemd-run --unit=karpo-promo-backup-v1 --property=RuntimeMaxSec=10800 --property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/backup.log --property=StandardError=inherit /bin/bash /opt/karpo/promo-backup-v1.sh
