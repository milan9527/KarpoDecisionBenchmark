set -Eeuo pipefail
if systemctl is-active --quiet karpo-rps-dualhost-humming-v1; then exit 1; fi
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/vllm-rps-fleet-v1.tar.gz /tmp/vllm-rps-fleet-v1.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 7749dcb1e90760584d060b1b33f37a5cc0682a34baec158d9e96628209698255 /tmp/vllm-rps-fleet-v1.tar.gz | sha256sum -c -
tar -xzf /tmp/vllm-rps-fleet-v1.tar.gz -C /opt/karpo
systemd-run --unit=karpo-rps-dual-node-v2 --property=RuntimeMaxSec=21600 --property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/dual-node-v2-launch.log --property=StandardError=inherit /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/scripts/vllm_rps_fleet.py --mode node --name dual-fleetnode-v2 --replica-ids 1 2 --batch 4
