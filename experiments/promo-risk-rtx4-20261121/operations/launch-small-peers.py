"""Allocate at most two same-region single-RTX Spot peers within 64 vCPU."""
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

OUT = Path(__file__).resolve().parent
ec2 = boto3.client("ec2", region_name="us-east-2",
                   config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 1}))
record = OUT / "small-peers.json"
peers = json.loads(record.read_text()) if record.exists() else []
assert len(peers) <= 2
original = ec2.describe_instances(InstanceIds=["i-0de4f061c2a04fe4b"])["Reservations"][0]["Instances"][0]
if original["State"]["Name"] not in ("stopped", "stopping"):
    raise SystemExit("The previous single-GPU controller must be stopping or stopped")
if original["State"]["Name"] == "stopping":
    diagnostic = json.loads((OUT / "single-stop-diagnostic.json").read_text())
    assert diagnostic["powerdown_lines"], "Require proof that the OS has powered down"
subnets = {
    s["AvailabilityZone"]: s["SubnetId"]
    for s in ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": ["vpc-0d829be5b32cedda7"]},
        {"Name": "default-for-az", "Values": ["true"]},
    ])["Subnets"]
}
log = OUT / "small-peer-capacity-attempts.json"
attempts = json.loads(log.read_text()) if log.exists() else []
for replica in (2, 3):
    if any(p["replica_id"] == replica for p in peers):
        continue
    for zone in ("us-east-2c", "us-east-2b", "us-east-2a"):
        try:
            response = ec2.run_instances(
                ImageId="ami-00ab7faabf42ddbe2", InstanceType="g7e.2xlarge", MinCount=1, MaxCount=1,
                ClientToken=f"karpo-promo-small-peer-{replica}-20261121-{zone}",
                SubnetId=subnets[zone], SecurityGroupIds=["sg-077c0629417a7de69"],
                KeyName="karpo-promo-dual-control-20261121",
                InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {
                    "SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop"}},
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                    "VolumeSize": 300, "VolumeType": "gp3", "Iops": 3000, "Throughput": 500,
                    "Encrypted": True, "DeleteOnTermination": False}}],
                TagSpecifications=[{"ResourceType": kind, "Tags": [
                    {"Key": "Name", "Value": f"karpo-promo-peer{replica}-20261121"},
                    {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                ]} for kind in ("instance", "volume")],
                UserData="#!/bin/bash\nshutdown -c || true\n",
            )
            i = response["Instances"][0]
            row = {
                "replica_id": replica, "region": "us-east-2", "az": zone, "instance_id": i["InstanceId"],
                "instance_type": i["InstanceType"], "private_ip": i["PrivateIpAddress"],
                "spot_request_id": i.get("SpotInstanceRequestId"), "market": "spot",
                "gpu_count": 1, "vcpus": 8, "management": "private_ssh_via_dual",
                "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            peers.append(row)
            record.write_text(json.dumps(peers, indent=2) + "\n")
            print(json.dumps(row), flush=True)
            break
        except ClientError as error:
            row = {"replica_id": replica, "zone": zone, "error": error.response["Error"]}
            attempts.append(row)
            log.write_text(json.dumps(attempts, indent=2) + "\n")
            print(json.dumps(row), flush=True)
            if error.response["Error"]["Code"] in ("MaxSpotInstanceCountExceeded", "MaxSpotFleetRequestCountExceeded"):
                raise SystemExit("Regional quota has not been released yet; retry after the stop completes")
            if error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
                raise
    else:
        raise SystemExit(f"No Spot capacity for replica {replica}; already allocated peers are recorded")
log.write_text(json.dumps(attempts, indent=2) + "\n")
