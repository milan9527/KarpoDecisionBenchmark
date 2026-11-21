"""Allocate exactly two additional single-RTX peers; retain all existing EC2."""
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[3]
EXP = ROOT / "experiments/promo-rtx6-20261121"
OPS = EXP / "operations"
REGION = "us-east-2"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=str, indent=2) + "\n")


def main():
    OPS.mkdir(parents=True, exist_ok=True)
    ec2 = boto3.client("ec2", region_name=REGION,
                       config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 1}))
    quota_client = boto3.client("service-quotas", region_name=REGION)
    quotas = {code: quota_client.get_service_quota(ServiceCode="ec2", QuotaCode=code)["Quota"]
              for code in ("L-3819A6DF", "L-DB2E81BA")}
    save(OPS / "quotas.json", quotas)
    existing = ec2.describe_instances(Filters=[
        {"Name": "tag:Project", "Values": ["KarpoDecisionBenchmark"]}])
    if not (OPS / "initial-infrastructure.json").exists():
        save(OPS / "initial-infrastructure.json", existing)
    reference = ec2.describe_instances(InstanceIds=["i-02c1caf7d7ce17c76"])["Reservations"][0]["Instances"][0]
    assert reference["State"]["Name"] == "running" and reference["InstanceType"] == "g7e.2xlarge"
    types = ec2.describe_instance_types(InstanceTypes=["g7e.2xlarge"])["InstanceTypes"]
    assert types[0]["GpuInfo"]["Gpus"][0]["Count"] == 1
    assert types[0]["VCpuInfo"]["DefaultVCpus"] == 8
    save(OPS / "larger-type-reference.json", types)
    subnets = {s["AvailabilityZone"]: s["SubnetId"] for s in ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [reference["VpcId"]]},
        {"Name": "default-for-az", "Values": ["true"]},
    ])["Subnets"]}
    target = OPS / "new-peers.json"
    peers = json.loads(target.read_text()) if target.exists() else []
    assert len(peers) <= 2
    attempts_path = OPS / "larger-single-capacity-attempts.json"
    attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else []
    for replica in (4, 5):
        if any(p["replica_id"] == replica for p in peers):
            continue
        for instance_type, zone in [(t,z) for z in ("us-east-2c","us-east-2b","us-east-2a") for t in ("g7e.4xlarge","g7e.8xlarge")]:
            try:
                response = ec2.run_instances(
                    ImageId=reference["ImageId"], InstanceType=instance_type, MinCount=1, MaxCount=1,
                    ClientToken=f"karpo-rtx6-peer-{replica}-{instance_type}-20261121-{zone}",
                    SubnetId=subnets[zone],
                    SecurityGroupIds=[g["GroupId"] for g in reference["SecurityGroups"]],
                    KeyName="karpo-promo-dual-control-20261121",
                    MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                    BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                        "VolumeSize": 300, "VolumeType": "gp3", "Iops": 3000, "Throughput": 500,
                        "Encrypted": True, "DeleteOnTermination": False}}],
                    TagSpecifications=[{"ResourceType": kind, "Tags": [
                        {"Key": "Name", "Value": f"karpo-rtx6-peer{replica}-20261121"},
                        {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                        {"Key": "Experiment", "Value": "promo-rtx6-20261121"},
                    ]} for kind in ("instance", "volume")],
                    UserData="#!/bin/bash\nshutdown -c || true\n",
                )
                instance = response["Instances"][0]
                row = {"replica_id": replica, "region": REGION, "az": zone,
                       "instance_id": instance["InstanceId"], "instance_type": instance["InstanceType"],
                       "private_ip": instance["PrivateIpAddress"], "market": "on-demand",
                       "gpu_count": 1, "vcpus": 16 if instance_type == "g7e.4xlarge" else 32, "management": "private_ssh_via_dual",
                       "utc": dt.datetime.now(dt.timezone.utc).isoformat()}
                peers.append(row)
                save(target, peers)
                print(json.dumps(row), flush=True)
                break
            except ClientError as error:
                attempts.append({"replica_id": replica, "zone": zone, "type": instance_type, "error": error.response["Error"]})
                save(attempts_path, attempts)
                if error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
                    raise
        else:
            raise RuntimeError("No same-region capacity; allocated peers are journaled")
    save(attempts_path, attempts)


if __name__ == "__main__":
    main()
