set -Eeuo pipefail
SSH_OPTIONS=(-i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts)
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/vllm-rps-fleet-v1.tar.gz /tmp/vllm-rps-fleet-v1.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
scp "${SSH_OPTIONS[@]}" /tmp/vllm-rps-fleet-v1.tar.gz ubuntu@172.31.25.147:/tmp/
ssh "${SSH_OPTIONS[@]}" ubuntu@172.31.25.147 'sudo bash -s' <<'REMOTE'
set -Eeuo pipefail
if systemctl is-active --quiet karpo-rps-h100-v1; then exit 1; fi
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
tar -xzf /tmp/vllm-rps-fleet-v1.tar.gz -C /opt/karpo
systemd-run --unit=karpo-rps-h100-node-v1 --property=RuntimeMaxSec=21600 --property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/h100-node-launch.log --property=StandardError=inherit /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_fleet.py --mode node --name h100-fleetnode-v1 --replica-ids 3 --batch 4 --roleless
REMOTE
