"""Prepare (or send) the retained FP8 runtime bootstrap for an approved GPU peer."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True)
parser.add_argument("--send", action="store_true")
args = parser.parse_args()
assert re.fullmatch(r"fleet[1-4]", args.name)
hosts = json.loads((ROOT / "experiments/vllm-rps100-20261120/results/fleet-instances.json").read_text())
host = next(h for h in hosts if h["name"] == args.name)
address = str(ipaddress.ip_address(host.get("ssh_address", host["PrivateIpAddress"])))
replica = host["replica_id"]
install = (STATE / "rps-install-h100.sh").read_text().replace("h100", args.name)
prefetch = (STATE / "rps-prefetch-h100.sh").read_text().replace("h100", args.name).replace(
    "/opt/karpo/venv-vllm034/bin/python", "/usr/bin/python3")
backup = (STATE / "rps-deploy-h100.sh").read_text().split(
    "cat > /opt/karpo/experiments/vllm-rps100-20261120/backup-h100.sh", 1)[1]
backup = "cat > /opt/karpo/experiments/vllm-rps100-20261120/backup-h100.sh" + backup
backup = backup.replace("h100", args.name).replace("172.31.25.147", address)
backup = backup.replace(f"/opt/karpo/.ssh/rps-{args.name}", "/opt/karpo/.ssh/rps-h100")

node = r'''
cat > /opt/karpo/experiments/vllm-rps100-20261120/launch-__NAME__-node.sh <<'PEER_NODE'
set -Eeuo pipefail
OUT=/opt/karpo/experiments/vllm-rps100-20261120/results/setup
exec >> "$OUT/__NAME__-node-launch.log" 2>&1
for attempt in $(seq 1 360); do
 if [ -f "$OUT/__NAME__-install-exit.txt" ]; then break; fi
 sleep 5
done
test "$(cat "$OUT/__NAME__-install-exit.txt")" = 0
/opt/karpo/venv-vllm034/bin/python -m pip install --no-deps /opt/karpo/serving/vllm_karpo
/opt/karpo/venv-vllm034/bin/python /opt/karpo/tests/test_vllm_rps.py > "$OUT/__NAME__-harness-tests.log" 2>&1
test ! -e /run/systemd/shutdown/scheduled
systemctl list-timers --all --no-pager > "$OUT/__NAME__-timers.txt"
nvidia-smi > "$OUT/__NAME__-nvidia-smi.txt"
exec /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_fleet.py --mode node --name __NAME__-fleetnode-v1 --replica-ids __REPLICA__ --batch 4 --roleless
PEER_NODE
systemd-run --unit=karpo-rps-__NAME__-node-v1 --property=RuntimeMaxSec=21600 /bin/bash /opt/karpo/experiments/vllm-rps100-20261120/launch-__NAME__-node.sh
'''
control = r'''
set -Eeuo pipefail
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
ready=false
for attempt in $(seq 1 90); do
 if ssh "${SSH_OPTIONS[@]}" ubuntu@__ADDRESS__ true; then ready=true; break; fi
 sleep 5
done
$ready
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/vllm-rps-fleet-v1.tar.gz /tmp/vllm-rps-fleet-v1.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
scp "${SSH_OPTIONS[@]}" /tmp/vllm-rps-fleet-v1.tar.gz ubuntu@__ADDRESS__:/tmp/
ssh "${SSH_OPTIONS[@]}" ubuntu@__ADDRESS__ 'sudo bash -s' <<'PEER_INITIALIZE'
set -Eeuo pipefail
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
tar -xzf /tmp/vllm-rps-fleet-v1.tar.gz -C /opt/karpo
'''
control += install + "\n" + prefetch + "\n" + node + "\nPEER_INITIALIZE\n" + backup
control = control.replace("__NAME__", args.name).replace("__ADDRESS__", address).replace(
    "__REPLICA__", str(replica))
launcher = (
    "set -Eeuo pipefail\n"
    f"cat > /opt/karpo/experiments/vllm-rps100-20261120/bootstrap-{args.name}.sh <<'PEER_CONTROL'\n"
    + control + "\nPEER_CONTROL\n"
    f"systemd-run --unit=karpo-rps-bootstrap-{args.name} --property=RuntimeMaxSec=1200 "
    f"--property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/{args.name}-control.log "
    "--property=StandardError=inherit /bin/bash "
    f"/opt/karpo/experiments/vllm-rps100-20261120/bootstrap-{args.name}.sh\n"
)
path = STATE / f"rps-bootstrap-{args.name}.sh"
path.write_text(launcher)
subprocess.run(["bash", "-n", str(path)], check=True)
print(path)
if args.send:
    subprocess.run(["python", str(STATE / "rps-cloud.py"), "send", str(path),
                    "--host", "single", "--seconds", "120"], check=True)
