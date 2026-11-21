"""Two same-model Spot peers after all same-region on-demand sizes failed."""
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

out = Path(__file__).resolve().parent
east2 = boto3.client("ec2", region_name="us-east-2")
ec2 = boto3.client("ec2", region_name="us-east-1",
                   config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 1}))
reference = ec2.describe_instances(InstanceIds=["i-05d0c24c739f41ca5"])["Reservations"][0]["Instances"][0]
dual = east2.describe_instances(InstanceIds=["i-05b24e3b4a7ee864d"])["Reservations"][0]["Instances"][0]
key_name = "karpo-rtx6-control-20261121"
try:
    key = ec2.describe_key_pairs(KeyNames=[key_name])["KeyPairs"][0]
except ClientError as error:
    if error.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
        raise
    public = east2.describe_key_pairs(
        KeyNames=["karpo-promo-dual-control-20261121"], IncludePublicKey=True)["KeyPairs"][0]["PublicKey"]
    key = ec2.import_key_pair(KeyName=key_name, PublicKeyMaterial=public.encode())
(out / "east1-key.json").write_text(json.dumps({k:key[k] for k in
    ["KeyName", "KeyFingerprint", "KeyPairId"]}, indent=2) + "\n")
groups = ec2.describe_security_groups(Filters=[
    {"Name": "vpc-id", "Values": [reference["VpcId"]]},
    {"Name": "group-name", "Values": ["karpo-rtx6-control-only-20261121"]},
])["SecurityGroups"]
if groups:
    group = groups[0]["GroupId"]
else:
    group = ec2.create_security_group(
        GroupName="karpo-rtx6-control-only-20261121", Description="SSH only from scoped RTX controller",
        VpcId=reference["VpcId"], TagSpecifications=[{"ResourceType": "security-group", "Tags": [
            {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
            {"Key": "Experiment", "Value": "promo-rtx6-20261121"},
        ]}])["GroupId"]
try:
    ec2.authorize_security_group_ingress(GroupId=group, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
        "IpRanges": [{"CidrIp": dual["PublicIpAddress"] + "/32", "Description": "RTX controller SSH tunnel"}],
    }])
except ClientError as error:
    if error.response["Error"]["Code"] != "InvalidPermission.Duplicate":
        raise
(out / "east1-network.json").write_text(json.dumps(
    {"group_id": group, "ssh_source": dual["PublicIpAddress"] + "/32",
     "inference_exposure": "loopback on peer; SSH tunnel only"}, indent=2) + "\n")
subnets = {s["AvailabilityZone"]: s["SubnetId"] for s in ec2.describe_subnets(Filters=[
    {"Name": "vpc-id", "Values": [reference["VpcId"]]},
    {"Name": "default-for-az", "Values": ["true"]},
])["Subnets"]}
target = out / "new-peers.json"
peers = json.loads(target.read_text()) if target.exists() else []
attempts = []
for replica in (4, 5):
    if any(p["replica_id"] == replica for p in peers):
        continue
    for zone in ("us-east-1b", "us-east-1d", "us-east-1c", "us-east-1a", "us-east-1f"):
        if zone not in subnets:
            continue
        try:
            response = ec2.run_instances(
                ImageId=reference["ImageId"], InstanceType="g7e.2xlarge", MinCount=1, MaxCount=1,
                ClientToken=f"karpo-rtx6-east1-peer-{replica}-20261121-{zone}",
                SubnetId=subnets[zone], SecurityGroupIds=[group], KeyName=key_name,
                InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {
                    "SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop"}},
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
            i = response["Instances"][0]
            row = {"replica_id": replica, "replica_ids": [replica], "region": "us-east-1", "az": zone,
                   "instance_id": i["InstanceId"], "instance_type": i["InstanceType"],
                   "private_ip": i["PrivateIpAddress"], "market": "spot",
                   "spot_request_id": i.get("SpotInstanceRequestId"), "gpu_count": 1, "vcpus": 8,
                   "management": "SSH via dual; inference via encrypted cross-region tunnel",
                   "utc": dt.datetime.now(dt.timezone.utc).isoformat()}
            peers.append(row)
            target.write_text(json.dumps(peers, indent=2) + "\n")
            print(json.dumps(row), flush=True)
            break
        except ClientError as error:
            attempts.append({"replica_id": replica, "zone": zone, "error": error.response["Error"]})
            (out / "east1-capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
            if error.response["Error"]["Code"] not in ("InsufficientInstanceCapacity", "Unsupported"):
                raise
    else:
        raise RuntimeError("No east1 single-RTX Spot capacity")
(out / "east1-capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
