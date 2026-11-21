"""Scoped management and evidence transport for the six-RTX validation."""
import argparse
import datetime as dt
import ipaddress
import json
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"
OPS = EXP / "operations"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"
HOSTS = {
    "dual": ("us-east-2", "i-05b24e3b4a7ee864d"),
}
CFG = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2})


def client(service, region):
    return boto3.client(service, region_name=region, config=CFG)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=str, ensure_ascii=False, indent=2) + "\n")


def hosts():
    result = {"dual": HOSTS["dual"]}
    old = ROOT / "experiments/promo-risk-rtx4-20261121/operations/small-peers.json"
    for path in (old, OPS / "new-peers.json"):
        if path.exists():
            for peer in json.loads(path.read_text()):
                result[f"peer{peer['replica_id']}"] = (peer["region"], peer["instance_id"])
    return result


def send(args):
    region, instance = hosts()[args.host]
    text = args.script.read_text()
    target = instance
    if args.host.startswith("peer"):
        info = client("ec2", region).describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]
        cross_region = region != HOSTS["dual"][0]
        address = str(ipaddress.ip_address(
            info["PublicIpAddress"] if cross_region else info["PrivateIpAddress"]))
        key_file = "/opt/karpo/.ssh/promo-rtx21"
        text = (
            f"ssh -i {key_file} -o BatchMode=yes -o ConnectTimeout=15 "
            "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts "
            f"ubuntu@{address} 'sudo /bin/bash -s' <<'PROMO_PEER'\n{text}\nPROMO_PEER"
        )
        region, instance = HOSTS["dual"]
    result = client("ssm", region).send_command(
        InstanceIds=[instance], DocumentName="AWS-RunShellScript",
        Parameters={"commands": ["/bin/bash -s <<'PROMO_COMMAND'\n" + text + "\nPROMO_COMMAND"],
                    "executionTimeout": [str(args.seconds)]}, TimeoutSeconds=args.seconds + 30,
    )
    cid = result["Command"]["CommandId"]
    save(OPS / "commands" / f"{cid}.json", {
        "id": cid, "host": args.host, "region": region, "instance": instance, "target": target,
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "script": text,
    })
    print(cid)


def read(cid):
    record = json.loads((OPS / "commands" / f"{cid}.json").read_text())
    result = client("ssm", record["region"]).get_command_invocation(CommandId=cid, InstanceId=record["instance"])
    save(OPS / "commands" / f"{cid}-result.json", result)
    print(result["Status"], result.get("StandardOutputContent", ""), result.get("StandardErrorContent", ""))


def status():
    rows = []
    for name, (region, instance) in hosts().items():
        i = client("ec2", region).describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]
        rows.append({"name": name, "region": region, "id": instance, "state": i["State"]["Name"],
                     "type": i["InstanceType"], "az": i["Placement"]["AvailabilityZone"],
                     "private_ip": i["PrivateIpAddress"], "public_ip": i.get("PublicIpAddress")})
    print(json.dumps(rows))


def sync():
    s3 = client("s3", "us-east-1")
    prefix = "results/" + EXP.name + "/"
    cache = ROOT / ".state/promo-rtx6-sync.json"
    previous = json.loads(cache.read_text()) if cache.exists() else {}
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            relative = Path(obj["Key"][len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid artifact path")
            path = EXP / relative
            if path.exists() and path.stat().st_size == obj["Size"] and previous.get(obj["Key"]) == obj["ETag"]:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(BUCKET, obj["Key"], str(path))
            previous[obj["Key"]] = obj["ETag"]
            count += 1
    save(cache, previous)
    print("downloaded", count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["send", "read", "status", "sync"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--host", choices=["dual", "peer2", "peer3", "peer4", "peer5"], default="dual")
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    if args.action == "send":
        args.script = Path(args.value)
        send(args)
    elif args.action == "read":
        read(args.value)
    elif args.action == "status":
        status()
    else:
        sync()
