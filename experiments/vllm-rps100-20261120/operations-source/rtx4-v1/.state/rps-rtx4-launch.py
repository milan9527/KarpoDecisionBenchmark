"""Provision exactly one additional RTX GPU for the user-approved four-RTX test."""
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/vllm-rps100-20261120/results"
PHASE = OUT / "rtx4"
PHASE.mkdir(exist_ok=True)
RECORD = OUT / "fleet-instances.json"
hosts = json.loads(RECORD.read_text()) if RECORD.exists() else []
if hosts:
    assert len(hosts) == 1 and hosts[0].get("purpose") == "homogeneous-rtx4"
    print(json.dumps(hosts[0]))
    raise SystemExit

cfg = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2})
ec2 = boto3.client("ec2", region_name="us-east-2", config=cfg)
pricing = boto3.client("pricing", region_name="us-east-1", config=cfg)
group = ec2.describe_security_groups(
    GroupIds=["sg-077c0629417a7de69"])["SecurityGroups"][0]
subnets = ec2.describe_subnets(
    Filters=[{"Name": "vpc-id", "Values": [group["VpcId"]]}])["Subnets"]
attempts = []
authorization = {
    "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "user_instruction": "用4个rtx",
    "gpu_model": "RTX PRO 6000 Blackwell Server Edition 96GB",
    "additional_gpus": 1,
    "retained_instances": [
        "i-0de4f061c2a04fe4b", "i-05b24e3b4a7ee864d", "i-091371593b3c4a5ff"],
    "operating_compute_cap_usd": 20,
    "cap_basis": "Prior assistant operating ceiling; no increase requested or inferred.",
    "ec2_shutdown": False,
    "spot_unavailable_reason": "G Spot quota 64 vCPU is fully used; pending increase not approved.",
}
(PHASE / "authorization.json").write_text(json.dumps(authorization, indent=2) + "\n")
spot = ec2.describe_spot_price_history(
    InstanceTypes=["g7e.4xlarge", "g7e.12xlarge", "p5.4xlarge"],
    ProductDescriptions=["Linux/UNIX"],
    StartTime=dt.datetime.now(dt.timezone.utc), MaxResults=100)["SpotPriceHistory"]
prices = {(r["InstanceType"], r["AvailabilityZone"]): float(r["SpotPrice"]) for r in spot}
retained = (prices["g7e.4xlarge", "us-east-2c"]
            + prices["g7e.12xlarge", "us-east-2c"]
            + prices["p5.4xlarge", "us-east-2b"])

for instance_type in ("g7e.4xlarge", "g7e.2xlarge", "g7e.8xlarge"):
    attributes = {
        "regionCode": "us-east-2", "instanceType": instance_type, "operatingSystem": "Linux",
        "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used"}
    products = pricing.get_products(ServiceCode="AmazonEC2", Filters=[
        {"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in attributes.items()
    ])["PriceList"]
    rates = {float(d["pricePerUnit"]["USD"]) for raw in products
             for term in json.loads(raw)["terms"]["OnDemand"].values()
             for d in term["priceDimensions"].values() if d["unit"] == "Hrs"}
    assert len(rates) == 1, rates
    price = rates.pop()
    assert retained + price <= 20, (retained, price)
    hardware = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
    assert sum(g["Count"] for g in hardware["GpuInfo"]["Gpus"]) == 1
    assert hardware["GpuInfo"]["TotalGpuMemoryInMiB"] == 98304
    for zone in ("us-east-2c", "us-east-2b", "us-east-2a"):
        subnet = next(s for s in subnets if s["AvailabilityZone"] == zone and s.get("DefaultForAz"))
        try:
            result = ec2.run_instances(
                ImageId="ami-00ab7faabf42ddbe2", InstanceType=instance_type,
                MinCount=1, MaxCount=1,
                ClientToken=f"karpo-rtx4-one-v1-20261120-{instance_type}-{zone}",
                SubnetId=subnet["SubnetId"], SecurityGroupIds=[group["GroupId"]],
                KeyName="karpo-rps100-private-20261120",
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
                "InstanceId", "InstanceType", "PrivateIpAddress", "Placement")}
            host.update(
                name="fleet1", purpose="homogeneous-rtx4", region="us-east-2",
                pricing_model="on-demand", hourly_compute_usd=price,
                projected_total_reference_usd=retained + price,
                replica_id=3, automatic_shutdown=False,
                management="private_ssh_via_single", iam_instance_role=None,
                utc=dt.datetime.now(dt.timezone.utc).isoformat())
            RECORD.write_text(json.dumps([host], indent=2, default=str) + "\n")
            (PHASE / "capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
            print(json.dumps(host, default=str))
            raise SystemExit
        except ClientError as error:
            attempts.append({"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "type": instance_type, "zone": zone, "error": error.response["Error"]})
            (PHASE / "capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
            print(json.dumps(attempts[-1]), flush=True)
            if error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
                raise
print(json.dumps({"status": "no_capacity", "attempts": len(attempts)}))
