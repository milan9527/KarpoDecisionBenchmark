"""Add one RTX worker in the nearby region, SSH restricted to the control host."""
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/vllm-rps100-20261120/results"
PHASE = OUT / "rtx4"
RECORD = OUT / "fleet-instances.json"
if RECORD.exists():
    hosts = json.loads(RECORD.read_text())
    assert len(hosts) == 1 and hosts[0]["purpose"] == "homogeneous-rtx4"
    print(json.dumps(hosts[0]))
    raise SystemExit
cfg = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 1})
ec2 = boto3.client("ec2", region_name="us-east-1", config=cfg)
authorization = json.loads((PHASE / "authorization.json").read_text())
assert authorization["user_instruction"] == "用4个rtx" and authorization["additional_gpus"] == 1
image_id = json.loads((PHASE / "ami-copy.json").read_text())["image_id"]
image = ec2.describe_images(ImageIds=[image_id])["Images"][0]
if image["State"] != "available":
    print(json.dumps({"image_id": image_id, "state": image["State"]}))
    raise SystemExit
source = boto3.client("ec2", region_name="us-east-2", config=cfg).describe_instances(
    InstanceIds=["i-0de4f061c2a04fe4b"])["Reservations"][0]["Instances"][0]
retained_prices = boto3.client("ec2", region_name="us-east-2", config=cfg).describe_spot_price_history(
    InstanceTypes=["g7e.4xlarge", "g7e.12xlarge", "p5.4xlarge"],
    ProductDescriptions=["Linux/UNIX"], StartTime=dt.datetime.now(dt.timezone.utc),
    MaxResults=100)["SpotPriceHistory"]
retained_rates = {(r["InstanceType"], r["AvailabilityZone"]): float(r["SpotPrice"])
                  for r in retained_prices}
retained_cost = (retained_rates["g7e.4xlarge", "us-east-2c"]
                 + retained_rates["g7e.12xlarge", "us-east-2c"]
                 + retained_rates["p5.4xlarge", "us-east-2b"])
assert retained_cost + 3 <= authorization["operating_compute_cap_usd"]
control_address = source["PublicIpAddress"]
vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]
subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["Subnets"]
group_name = "karpo-rtx4-control-only-20261120"
groups = ec2.describe_security_groups(Filters=[
    {"Name": "group-name", "Values": [group_name]},
    {"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["SecurityGroups"]
if groups:
    group_id = groups[0]["GroupId"]
else:
    group_id = ec2.create_security_group(
        GroupName=group_name, Description="SSH solely from retained Karpo control host",
        VpcId=vpc["VpcId"])["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
        "IpRanges": [{"CidrIp": control_address + "/32", "Description": "Retained Karpo control host"}],
    }])
key_name = "karpo-rtx4-control-20261120"
try:
    ec2.describe_key_pairs(KeyNames=[key_name])
except ClientError as error:
    if error.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
        raise
    ec2.import_key_pair(KeyName=key_name, PublicKeyMaterial=(OUT / "setup/control-public-key.txt").read_bytes())
prices = ec2.describe_spot_price_history(
    InstanceTypes=["g7e.4xlarge"], ProductDescriptions=["Linux/UNIX"],
    StartTime=dt.datetime.now(dt.timezone.utc), MaxResults=100)["SpotPriceHistory"]
attempts = []
for row in sorted(prices, key=lambda p: float(p["SpotPrice"])):
    zone, price = row["AvailabilityZone"], float(row["SpotPrice"])
    if price > 3:
        continue
    subnet = next(s for s in subnets if s["AvailabilityZone"] == zone and s.get("DefaultForAz"))
    try:
        result = ec2.run_instances(
            ImageId=image_id, InstanceType="g7e.4xlarge", MinCount=1, MaxCount=1,
            ClientToken=f"karpo-rtx4-east1-spot-v1-20261120-{zone}",
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet["SubnetId"],
                                "AssociatePublicIpAddress": True, "Groups": [group_id],
                                "DeleteOnTermination": True}],
            KeyName=key_name,
            InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {
                "SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop",
                "MaxPrice": "3.00"}},
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                "VolumeSize": 300, "VolumeType": "gp3", "Iops": 3000, "Throughput": 500,
                "Encrypted": True, "DeleteOnTermination": False}}],
            TagSpecifications=[{"ResourceType": kind, "Tags": [
                {"Key": "Name", "Value": "karpo-rtx4-fleet1-20261120"},
                {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                {"Key": "KeepRunning", "Value": "User requested EC2 remain running"},
            ]} for kind in ("instance", "volume")],
            UserData="#!/bin/bash\nset -u\nshutdown -c || true\n"
                     "for unit in $(systemctl list-unit-files --no-legend 'karpo*.timer' | awk '{print $1}'); do\n"
                     " systemctl disable --now \"$unit\" || true\n"
                     "done\n")
        instance = result["Instances"][0]
        host = {k: instance.get(k) for k in (
            "InstanceId", "InstanceType", "PrivateIpAddress", "PublicIpAddress", "Placement")}
        host.update(
            name="fleet1", purpose="homogeneous-rtx4", region="us-east-1", replica_id=3,
            pricing_model="spot", spot_price_reference_usd=price, maximum_spot_bid_usd=3,
            projected_total_reference_usd=retained_cost + price,
            automatic_shutdown=False, management="restricted_ssh_via_single", iam_instance_role=None,
            control_source_address=control_address, security_group_id=group_id,
            inference_access="SSH local forward on control host 127.0.0.1:18503; no public inference port",
            utc=dt.datetime.now(dt.timezone.utc).isoformat())
        if host.get("PublicIpAddress"):
            host["ssh_address"] = host["PublicIpAddress"]
        RECORD.write_text(json.dumps([host], indent=2, default=str) + "\n")
        (PHASE / "east1-capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
        print(json.dumps(host, default=str))
        raise SystemExit
    except ClientError as error:
        attempts.append({"zone": zone, "error": error.response["Error"]})
        (PHASE / "east1-capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
        if error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
            raise
print(json.dumps({"status": "no_capacity", "attempts": attempts}))
