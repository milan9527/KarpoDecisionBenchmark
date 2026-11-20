"""Provision one homogeneous H200 host and keep evidence separate from RTX runs."""
import argparse
import datetime as dt
import ipaddress
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/vllm-sop-h200-20261120/results"
OUT.mkdir(parents=True, exist_ok=True)
BUCKET = "karpo-benchmark-20261119-115003-632930644527"
CONTROL = "i-0de4f061c2a04fe4b"
CFG = Config(connect_timeout=8, read_timeout=30, retries={"mode": "standard", "max_attempts": 3})


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, default=str, ensure_ascii=False, indent=2) + "\n")


def launch():
    path = OUT / "instance.json"
    if path.exists():
        print(path.read_text())
        return
    ec2 = boto3.client("ec2", region_name="us-east-2", config=CFG)
    group = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": ["karpo-rps100-h100-private-20261120"]}
    ])["SecurityGroups"][0]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [group["VpcId"]]}])["Subnets"]
    existing = [i for r in ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": ["karpo-sop-h200-20261120"]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}
    ])["Reservations"] for i in r["Instances"]]
    if existing:
        raise RuntimeError("An H200 host already exists; recover its record before launching another.")
    rows = ec2.describe_spot_price_history(
        InstanceTypes=["p5en.48xlarge", "p5e.48xlarge"], ProductDescriptions=["Linux/UNIX"],
        StartTime=dt.datetime.now(dt.timezone.utc), MaxResults=100
    )["SpotPriceHistory"]
    attempts = []
    for row in sorted(rows, key=lambda r: float(r["SpotPrice"])):
        if float(row["SpotPrice"]) > 40:
            continue
        zone = row["AvailabilityZone"]
        matches = [s for s in subnets if s["AvailabilityZone"] == zone and s.get("DefaultForAz")]
        if not matches:
            continue
        try:
            response = ec2.run_instances(
                ImageId="ami-00ab7faabf42ddbe2", InstanceType=row["InstanceType"], MinCount=1, MaxCount=1,
                ClientToken=f"karpo-sop-h200-20261120-{row['InstanceType']}-{zone}",
                SubnetId=matches[0]["SubnetId"], SecurityGroupIds=[group["GroupId"]],
                KeyName="karpo-rps100-private-20261120",
                InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {
                    "SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop", "MaxPrice": "40"}},
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                    "VolumeSize": 300, "VolumeType": "gp3", "Iops": 3000, "Throughput": 500,
                    "Encrypted": True, "DeleteOnTermination": False}}],
                TagSpecifications=[{"ResourceType": kind, "Tags": [
                    {"Key": "Name", "Value": "karpo-sop-h200-20261120"},
                    {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                    {"Key": "Scenario", "Value": "SOP-H200"},
                    {"Key": "KeepRunning", "Value": "User requested retained EC2"}
                ]} for kind in ("instance", "volume")],
                UserData=(
                    "#!/bin/bash\nset -u\nshutdown -c || true\n"
                    "for unit in $(systemctl list-unit-files --no-legend 'karpo*.timer' | awk '{print $1}'); do\n"
                    " systemctl disable --now \"$unit\" || true\ndone\n"
                ),
            )
            i = response["Instances"][0]
            host = {k: i.get(k) for k in ("InstanceId", "InstanceType", "PrivateIpAddress", "Placement")}
            host.update(region="us-east-2", gpu_model="H200", gpu_count=8,
                        market="spot", reference_usd_per_hour=float(row["SpotPrice"]), max_spot_usd_per_hour=40,
                        on_demand_usd_per_hour=63.296, automatic_ec2_shutdown=False,
                        management="private_ssh_via_control", security_group=group["GroupId"],
                        utc=dt.datetime.now(dt.timezone.utc).isoformat())
            save(path, host)
            save(OUT / "capacity-attempts.json", attempts)
            print(json.dumps(host, default=str))
            return
        except ClientError as error:
            attempts.append({"type": row["InstanceType"], "az": zone, "error": error.response["Error"]})
            save(OUT / "capacity-attempts.json", attempts)
            if error.response["Error"]["Code"] not in ("InsufficientInstanceCapacity", "SpotMaxPriceTooLow"):
                raise
    print(json.dumps({"status": "no_spot_capacity", "attempts": attempts}))


def send(script, seconds, control=False):
    text = Path(script).read_text()
    if not control:
        host = json.loads((OUT / "instance.json").read_text())
        address = str(ipaddress.ip_address(host["PrivateIpAddress"]))
        text = (
            "ssh -i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 "
            "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts "
            f"ubuntu@{address} 'sudo /bin/bash -s' <<'H200_PEER'\n{text}\nH200_PEER\n"
        )
    ssm = boto3.client("ssm", region_name="us-east-2", config=CFG)
    response = ssm.send_command(
        InstanceIds=[CONTROL], DocumentName="AWS-RunShellScript",
        Parameters={"commands": ["/bin/bash -s <<'H200_COMMAND'\n" + text + "\nH200_COMMAND"],
                    "executionTimeout": [str(seconds)]},
        TimeoutSeconds=seconds + 30)
    cid = response["Command"]["CommandId"]
    save(OUT / "commands" / f"{cid}.json",
         {"id": cid, "control": CONTROL, "script": text, "utc": dt.datetime.now(dt.timezone.utc).isoformat()})
    print(cid)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["launch", "status", "send", "read", "sync"])
    p.add_argument("value", nargs="?")
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--control", action="store_true")
    a = p.parse_args()
    if a.action == "launch":
        launch()
    elif a.action == "send":
        send(a.value, a.seconds, a.control)
    elif a.action == "read":
        result = boto3.client("ssm", region_name="us-east-2", config=CFG).get_command_invocation(
            CommandId=a.value, InstanceId=CONTROL)
        save(OUT / "commands" / f"{a.value}-result.json", result)
        print(result["Status"], result.get("StandardOutputContent", ""), result.get("StandardErrorContent", ""))
    elif a.action == "status":
        host = json.loads((OUT / "instance.json").read_text())
        ec2 = boto3.client("ec2", region_name=host["region"], config=CFG)
        i = ec2.describe_instances(InstanceIds=[host["InstanceId"]])["Reservations"][0]["Instances"][0]
        print(json.dumps({"id": i["InstanceId"], "state": i["State"]["Name"], "type": i["InstanceType"],
                          "private_ip": i["PrivateIpAddress"], "az": i["Placement"]["AvailabilityZone"]}))
    else:
        s3 = boto3.client("s3", region_name="us-east-1", config=CFG)
        prefix = "results/vllm-sop-h200-20261120/"
        cache_path = ROOT / ".state/sop-h200-etags.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        count = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                target = OUT / obj["Key"][len(prefix):]
                if target.exists() and target.stat().st_size == obj["Size"] and cache.get(obj["Key"]) == obj["ETag"]:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(BUCKET, obj["Key"], str(target))
                cache[obj["Key"]] = obj["ETag"]
                count += 1
        save(cache_path, cache)
        print("downloaded", count)


if __name__ == "__main__":
    main()
