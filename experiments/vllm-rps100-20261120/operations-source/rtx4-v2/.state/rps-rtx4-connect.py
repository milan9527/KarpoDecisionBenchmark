"""Refresh the fourth RTX endpoint and prepare its localhost-only SSH forwarding."""
import json
from pathlib import Path
import subprocess
import ipaddress

import boto3

ROOT = Path(__file__).resolve().parents[1]
record = ROOT / "experiments/vllm-rps100-20261120/results/fleet-instances.json"
hosts = json.loads(record.read_text())
assert len(hosts) == 1
host = hosts[0]
assert host["purpose"] == "homogeneous-rtx4" and host["region"] == "us-east-1"
ec2 = boto3.client("ec2", region_name=host["region"])
instance = ec2.describe_instances(InstanceIds=[host["InstanceId"]])["Reservations"][0]["Instances"][0]
assert instance["State"]["Name"] == "running", instance["State"]
address = str(ipaddress.ip_address(instance["PublicIpAddress"]))
host.update(PrivateIpAddress=instance["PrivateIpAddress"], PublicIpAddress=address, ssh_address=address)
record.write_text(json.dumps(hosts, indent=2) + "\n")
script = f"""set -Eeuo pipefail
systemd-run --unit=karpo-rps-rtx4-tunnel-v1 --property=RuntimeMaxSec=21600 --property=Restart=on-failure --property=RestartSec=5 --property=StandardOutput=append:/opt/karpo/experiments/vllm-rps100-20261120/results/setup/rtx4-tunnel.log --property=StandardError=inherit /usr/bin/ssh -N -T -i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:18503:127.0.0.1:18500 ubuntu@{address}
"""
path = ROOT / ".state/rps-rtx4-tunnel.sh"
path.write_text(script)
subprocess.run(["bash", "-n", str(path)], check=True)
print(json.dumps({"instance": host["InstanceId"], "address": address, "script": str(path)}))
