set -Eeuo pipefail
cat > /opt/karpo/promo-peer3-bootstrap.sh <<'PROMO_BOOTSTRAP_SCRIPT'
set -Eeuo pipefail
PROMO_SSH=(-i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${PROMO_SSH[@]}" ubuntu@172.31.37.239 true; then ready=true; break; fi
 sleep 5
done
$ready
ssh "${PROMO_SSH[@]}" ubuntu@172.31.37.239 'sudo /bin/bash -s' <<'PROMO_PEER_GUARD'
set -Eeuo pipefail
shutdown -c || true
systemctl list-unit-files --no-legend | while read -r unit rest; do
 if [[ "$unit" == karpo*.timer ]]; then systemctl disable --now "$unit"; fi
done
mkdir -p /opt/karpo/venv-vllm034
PROMO_PEER_GUARD
rsync -a --exclude=__pycache__ -e "ssh ${PROMO_SSH[*]}" --rsync-path='sudo rsync' /opt/karpo/venv-vllm034/ ubuntu@172.31.37.239:/opt/karpo/venv-vllm034/
scp "${PROMO_SSH[@]}" /tmp/promo-risk-rtx4-v1.tar.gz ubuntu@172.31.37.239:/tmp/promo-risk-rtx4-v1.tar.gz
ssh "${PROMO_SSH[@]}" ubuntu@172.31.37.239 'sudo /bin/bash -s' <<'PROMO_PEER_INITIALIZE'
set -Eeuo pipefail
PROMO_ROOT=/opt/karpo/promo-validation-v1
PROMO_PY=/opt/karpo/venv-vllm034/bin/python
printf '%s  %s\n' 'dd0b37780b7e34fe2f1630466784ffbd5a29e48cf3c9a1f6c1e6d091a503e52b' /tmp/promo-risk-rtx4-v1.tar.gz | sha256sum -c -
mkdir -p "$PROMO_ROOT"
tar -xzf /tmp/promo-risk-rtx4-v1.tar.gz -C "$PROMO_ROOT"
ln -s /opt/karpo/models "$PROMO_ROOT/models"
cd "$PROMO_ROOT"
mkdir -p experiments/promo-risk-rtx4-20261121/results/setup
"$PROMO_PY" -m pip check > experiments/promo-risk-rtx4-20261121/results/setup/peer3-pip-check.log 2>&1
PYTHONPATH="$PROMO_ROOT/tests" "$PROMO_PY" -m unittest test_vllm_karpo.MarkerPoolingTests > experiments/promo-risk-rtx4-20261121/results/setup/peer3-adapter-tests.log 2>&1
test ! -e /run/systemd/shutdown/scheduled
systemd-run --unit=karpo-promo-peer3-v1 --property=RuntimeMaxSec=10800 --property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/peer3-node-launch.log --property=StandardError=inherit "$PROMO_PY" -u "$PROMO_ROOT/scripts/promo_rtx4_suite.py" node --name peer3-v1 --replica-ids 3 --bind 0.0.0.0
PROMO_PEER_INITIALIZE

PROMO_BOOTSTRAP_SCRIPT
systemd-run --unit=karpo-promo-bootstrap-peer3-v1b --property=RuntimeMaxSec=2400 --property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/peer3-bootstrap.log --property=StandardError=inherit /bin/bash /opt/karpo/promo-peer3-bootstrap.sh
