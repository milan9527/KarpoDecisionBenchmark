"""Write deployment scripts using only journaled same-region RTX peers."""
import ipaddress
import json
from pathlib import Path
import subprocess

OUT = Path(__file__).resolve().parent
peers = json.loads((OUT / "small-peers.json").read_text())
sha = json.loads((OUT.parent / "bundle-v1.json").read_text())["sha256"]
for peer in peers:
    replica = peer["replica_id"]
    address = str(ipaddress.ip_address(peer["private_ip"]))
    body = r"""set -Eeuo pipefail
PROMO_SSH=(-i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${PROMO_SSH[@]}" ubuntu@__ADDRESS__ true; then ready=true; break; fi
 sleep 5
done
$ready
ssh "${PROMO_SSH[@]}" ubuntu@__ADDRESS__ 'sudo /bin/bash -s' <<'PROMO_PEER_GUARD'
set -Eeuo pipefail
shutdown -c || true
systemctl list-unit-files --no-legend | while read -r unit rest; do
 if [[ "$unit" == karpo*.timer ]]; then systemctl disable --now "$unit"; fi
done
mkdir -p /opt/karpo/venv-vllm034
PROMO_PEER_GUARD
rsync -a --exclude=__pycache__ -e "ssh ${PROMO_SSH[*]}" --rsync-path='sudo rsync' /opt/karpo/venv-vllm034/ ubuntu@__ADDRESS__:/opt/karpo/venv-vllm034/
scp "${PROMO_SSH[@]}" /tmp/promo-risk-rtx4-v1.tar.gz ubuntu@__ADDRESS__:/tmp/promo-risk-rtx4-v1.tar.gz
ssh "${PROMO_SSH[@]}" ubuntu@__ADDRESS__ 'sudo /bin/bash -s' <<'PROMO_PEER_INITIALIZE'
set -Eeuo pipefail
PROMO_ROOT=/opt/karpo/promo-validation-v1
PROMO_PY=/opt/karpo/venv-vllm034/bin/python
printf '%s  %s\n' '__SHA__' /tmp/promo-risk-rtx4-v1.tar.gz | sha256sum -c -
mkdir -p "$PROMO_ROOT"
tar -xzf /tmp/promo-risk-rtx4-v1.tar.gz -C "$PROMO_ROOT"
ln -s /opt/karpo/models "$PROMO_ROOT/models"
cd "$PROMO_ROOT"
mkdir -p experiments/promo-risk-rtx4-20261121/results/setup
"$PROMO_PY" -m pip check > experiments/promo-risk-rtx4-20261121/results/setup/peer__REPLICA__-pip-check.log 2>&1
PYTHONPATH="$PROMO_ROOT/tests" "$PROMO_PY" -m unittest test_vllm_karpo.MarkerPoolingTests > experiments/promo-risk-rtx4-20261121/results/setup/peer__REPLICA__-adapter-tests.log 2>&1
test ! -e /run/systemd/shutdown/scheduled
systemd-run --unit=karpo-promo-peer__REPLICA__-v1 --property=RuntimeMaxSec=10800 --property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/peer__REPLICA__-node-launch.log --property=StandardError=inherit "$PROMO_PY" -u "$PROMO_ROOT/scripts/promo_rtx4_suite.py" node --name peer__REPLICA__-v1 --replica-ids __REPLICA__ --bind 0.0.0.0
PROMO_PEER_INITIALIZE
"""
    body = body.replace("__ADDRESS__", address).replace("__REPLICA__", str(replica)).replace("__SHA__", sha)
    inner = f"/opt/karpo/promo-peer{replica}-bootstrap.sh"
    script = (
        "set -Eeuo pipefail\n"
        f"cat > {inner} <<'PROMO_BOOTSTRAP_SCRIPT'\n{body}\nPROMO_BOOTSTRAP_SCRIPT\n"
        f"systemd-run --unit=karpo-promo-bootstrap-peer{replica}-v1b --property=RuntimeMaxSec=2400 "
        "--property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/"
        f"peer{replica}-bootstrap.log --property=StandardError=inherit /bin/bash {inner}\n"
    )
    path = OUT / f"bootstrap-peer{replica}.sh"
    path.write_text(script)
    subprocess.run(["bash", "-n", str(path)], check=True)
    print(path)
