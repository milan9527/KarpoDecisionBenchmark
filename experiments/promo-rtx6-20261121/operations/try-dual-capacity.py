"""Same-region two-RTX fallback after small on-demand instances were unavailable."""
import datetime as dt
import json
from pathlib import Path
import boto3
from botocore.exceptions import ClientError

out = Path(__file__).resolve().parent
assert not (out / "new-peers.json").exists()
ec2 = boto3.client("ec2", region_name="us-east-2")
reference = ec2.describe_instances(InstanceIds=["i-02c1caf7d7ce17c76"])["Reservations"][0]["Instances"][0]
subnets = {s["AvailabilityZone"]: s["SubnetId"] for s in ec2.describe_subnets(Filters=[
    {"Name": "vpc-id", "Values": [reference["VpcId"]]},
    {"Name": "default-for-az", "Values": ["true"]},
])["Subnets"]}
info = ec2.describe_instance_types(InstanceTypes=["g7e.12xlarge"])["InstanceTypes"][0]
assert sum(g["Count"] for g in info["GpuInfo"]["Gpus"]) == 2
attempts = []
for zone in ("us-east-2c", "us-east-2b", "us-east-2a"):
    try:
        response = ec2.run_instances(
            ImageId=reference["ImageId"], InstanceType="g7e.12xlarge", MinCount=1, MaxCount=1,
            ClientToken=f"karpo-rtx6-dual-20261121-{zone}", SubnetId=subnets[zone],
            SecurityGroupIds=[g["GroupId"] for g in reference["SecurityGroups"]],
            KeyName="karpo-promo-dual-control-20261121",
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                "VolumeSize": 300, "VolumeType": "gp3", "Iops": 3000, "Throughput": 500,
                "Encrypted": True, "DeleteOnTermination": False}}],
            TagSpecifications=[{"ResourceType": kind, "Tags": [
                {"Key": "Name", "Value": "karpo-rtx6-extra-dual-20261121"},
                {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                {"Key": "Experiment", "Value": "promo-rtx6-20261121"},
            ]} for kind in ("instance", "volume")], UserData="#!/bin/bash\nshutdown -c || true\n",
        )
        i = response["Instances"][0]
        row = {"replica_id": 4, "replica_ids": [4, 5], "region": "us-east-2", "az": zone,
               "instance_id": i["InstanceId"], "instance_type": i["InstanceType"],
               "private_ip": i["PrivateIpAddress"], "market": "on-demand", "gpu_count": 2,
               "vcpus": 48, "management": "private_ssh_via_dual",
               "utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        (out / "new-peers.json").write_text(json.dumps([row], indent=2) + "\n")
        print(json.dumps(row), flush=True)
        break
    except ClientError as error:
        attempts.append({"zone": zone, "type": "g7e.12xlarge", "error": error.response["Error"]})
        if error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
            raise
else:
    print("No same-region two-RTX on-demand capacity")
(out / "dual-capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
