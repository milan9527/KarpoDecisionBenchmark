"""Add a bounded, roleless private GPU worker; existing EC2s stay running."""
import argparse
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

OUT = Path(__file__).resolve().parents[1] / "experiments/vllm-rps100-20261120/results"
authorization = OUT / "budget-authorization.json"
if not authorization.exists():
    raise SystemExit("The proposed 30 USD/hour budget is awaiting the user's answer; no EC2 created.")
assert json.loads(authorization.read_text())["hourly_compute_cap_usd"] >= 30
parser = argparse.ArgumentParser()
parser.add_argument("--number", type=int, required=True)
args = parser.parse_args()
assert 1 <= args.number <= 4
record = OUT / "fleet-instances.json"
hosts = json.loads(record.read_text()) if record.exists() else []
name = f"fleet{args.number}"
if any(h["name"] == name for h in hosts):
    print(json.dumps(next(h for h in hosts if h["name"] == name)))
    raise SystemExit
ec2 = boto3.client("ec2", region_name="us-east-2")
price_rows = ec2.describe_spot_price_history(
    InstanceTypes=["g7e.4xlarge", "g7e.12xlarge", "p5.4xlarge"],
    ProductDescriptions=["Linux/UNIX"], StartTime=dt.datetime.now(dt.timezone.utc),
    MaxResults=100)["SpotPriceHistory"]
prices = {(r["InstanceType"], r["AvailabilityZone"]): float(r["SpotPrice"]) for r in price_rows}
retained_reference = (
    prices["g7e.4xlarge", "us-east-2c"] + prices["g7e.12xlarge", "us-east-2c"]
    + prices["p5.4xlarge", "us-east-2b"]
    + sum(prices[h["InstanceType"], h["Placement"]["AvailabilityZone"]] for h in hosts)
)
group = ec2.describe_security_groups(Filters=[
    {"Name": "group-name", "Values": ["karpo-rps100-h100-private-20261120"]}
])["SecurityGroups"][0]
try:
    ec2.authorize_security_group_ingress(GroupId=group["GroupId"], IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 18500, "ToPort": 18515,
        "UserIdGroupPairs": [{"GroupId": "sg-015c45dfc878c7919", "Description": "Private benchmark requests"}],
    }])
except ClientError as error:
    if error.response["Error"]["Code"] != "InvalidPermission.Duplicate":
        raise
subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [group["VpcId"]]}])["Subnets"]
attempts = []
for zone in ("us-east-2b", "us-east-2c", "us-east-2a"):
    choices = [s for s in subnets if s["AvailabilityZone"] == zone and s.get("DefaultForAz")]
    if not choices:
        continue
    reference_price = prices["p5.4xlarge", zone]
    if reference_price > 4.75:
        attempts.append({"zone": zone, "status": "above_spot_bid_limit", "price": reference_price})
        continue
    projected = retained_reference + reference_price
    if projected > 30:
        raise RuntimeError(f"Proposed compute reference {projected:.2f}/h exceeds 30/h plan")
    try:
        result = ec2.run_instances(
            ImageId="ami-00ab7faabf42ddbe2", InstanceType="p5.4xlarge", MinCount=1, MaxCount=1,
            ClientToken=f"karpo-rps100-{name}-h100-spot-20261120-{zone}", SubnetId=choices[0]["SubnetId"],
            SecurityGroupIds=[group["GroupId"]], KeyName="karpo-rps100-private-20261120",
            InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop", "MaxPrice": "4.75"}},
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                "VolumeSize": 300, "VolumeType": "gp3", "Iops": 3000, "Throughput": 500,
                "Encrypted": True, "DeleteOnTermination": False,
            }}],
            TagSpecifications=[{"ResourceType": kind, "Tags": [
                {"Key": "Name", "Value": f"karpo-rps100-{name}-20261120"},
                {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                {"Key": "KeepRunning", "Value": "User requested no EC2 shutdown before good results"},
            ]} for kind in ("instance", "volume")],
            UserData="#!/bin/bash\nset -u\nshutdown -c || true\n"
                     "for unit in $(systemctl list-unit-files --no-legend 'karpo*.timer' | awk '{print $1}'); do\n"
                     " systemctl disable --now \"$unit\" || true\n"
                     "done\n",
        )
        instance = result["Instances"][0]
        host = {key: instance.get(key) for key in ("InstanceId", "InstanceType", "PrivateIpAddress", "Placement")}
        host.update(name=name, region="us-east-2", on_demand_reference_usd=6.88, pricing_model="spot",
                    spot_price_reference_usd=reference_price, maximum_spot_bid_usd=4.75,
                    projected_total_reference_usd=projected,
                    replica_id=3+args.number,
                    automatic_shutdown=False, management="private_ssh_via_single", iam_instance_role=None,
                    utc=dt.datetime.now(dt.timezone.utc).isoformat())
        hosts.append(host)
        record.write_text(json.dumps(hosts, indent=2, default=str) + "\n")
        print(json.dumps(host, default=str))
        break
    except ClientError as error:
        attempts.append({"zone": zone, "error": error.response["Error"]})
        (OUT / f"{name}-capacity-attempts.json").write_text(json.dumps(attempts, indent=2) + "\n")
        if error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
            raise
else:
    print(json.dumps({"status": "no_capacity", "attempts": attempts}))
