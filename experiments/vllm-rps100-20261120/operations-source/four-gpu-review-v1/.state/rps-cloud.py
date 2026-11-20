import argparse
import datetime as dt
import json
import ipaddress
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/vllm-rps100-20261120/results"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"
HOSTS = {"single": "i-0de4f061c2a04fe4b", "dual": "i-05b24e3b4a7ee864d"}
HOST_META = {}
OUT.mkdir(parents=True, exist_ok=True)
if (OUT / "h100-instance.json").exists():
    HOST_META["h100"] = json.loads((OUT / "h100-instance.json").read_text())
    HOSTS["h100"] = HOST_META["h100"]["InstanceId"]
if (OUT / "fleet-instances.json").exists():
    for host in json.loads((OUT / "fleet-instances.json").read_text()):
        HOST_META[host["name"]] = host
        HOSTS[host["name"]] = host["InstanceId"]
cfg = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2})
ec2 = boto3.client("ec2", region_name="us-east-2", config=cfg)
ssm = boto3.client("ssm", region_name="us-east-2", config=cfg)
p = argparse.ArgumentParser()
p.add_argument("action", choices=["start", "status", "send", "read", "sync"])
p.add_argument("value", nargs="?")
p.add_argument("--host", default="single", choices=HOSTS)
p.add_argument("--seconds", type=int, default=180)
a = p.parse_args()
if a.action == "start":
    result = ec2.start_instances(InstanceIds=[HOSTS[a.host]])
    (OUT / f"start-{a.host}.json").write_text(json.dumps(result, default=str, indent=2) + "\n")
    print(json.dumps(result["StartingInstances"]))
elif a.action == "status":
    instances = [i for r in ec2.describe_instances(InstanceIds=list(HOSTS.values()))["Reservations"]
                 for i in r["Instances"]]
    online = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": list(HOSTS.values())}])
    print(json.dumps({"instances": [{"id": i["InstanceId"], "state": i["State"]["Name"],
                                    "type": i["InstanceType"], "ip": i["PrivateIpAddress"]} for i in instances],
                      "ssm": [{"id": i["InstanceId"], "ping": i["PingStatus"]} for i in online["InstanceInformationList"]]}))
elif a.action == "send":
    text = Path(a.value).read_text()
    control_instance = HOSTS[a.host]
    if a.host in HOST_META:
        host = HOST_META[a.host]
        if host.get("management") == "private_ssh_via_single":
            address = str(ipaddress.ip_address(host["PrivateIpAddress"]))
            text = (
                "ssh -i /opt/karpo/.ssh/rps-h100 -o BatchMode=yes -o ConnectTimeout=15 "
                "-o StrictHostKeyChecking=accept-new "
                "-o UserKnownHostsFile=/opt/karpo/.ssh/rps-known-hosts "
                f"ubuntu@{address} 'sudo /bin/bash -s' <<'RPS_SSH_INNER'\n"
                + text + "\nRPS_SSH_INNER"
            )
            control_instance = HOSTS["single"]
    result = ssm.send_command(
        InstanceIds=[control_instance], DocumentName="AWS-RunShellScript",
        Parameters={"commands": ["/bin/bash -s <<'RPS_COMMAND'\n" + text + "\nRPS_COMMAND"],
                    "executionTimeout": [str(a.seconds)]}, TimeoutSeconds=a.seconds + 30)
    cid = result["Command"]["CommandId"]
    commands = OUT / "commands"
    commands.mkdir(exist_ok=True)
    (commands / f"{cid}.json").write_text(json.dumps({
        "id": cid, "host": a.host, "instance": control_instance, "target_instance": HOSTS[a.host], "script": text,
        "utc": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2) + "\n")
    print(cid)
elif a.action == "read":
    record = json.loads((OUT / "commands" / f"{a.value}.json").read_text())
    result = ssm.get_command_invocation(CommandId=a.value, InstanceId=record["instance"])
    (OUT / "commands" / f"{a.value}-result.json").write_text(json.dumps(result, default=str, indent=2) + "\n")
    print(result["Status"], result.get("StandardOutputContent", ""), result.get("StandardErrorContent", ""))
else:
    s3 = boto3.client("s3", region_name="us-east-1", config=cfg)
    prefix = "results/vllm-rps100-20261120/"
    cache_path = ROOT / ".state/rps-sync-etags.json"
    etags = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    changed = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            path = OUT / obj["Key"][len(prefix):]
            if path.exists() and path.stat().st_size == obj["Size"] and etags.get(obj["Key"]) == obj["ETag"]:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(BUCKET, obj["Key"], str(path))
            etags[obj["Key"]] = obj["ETag"]
            changed += 1
    cache_path.write_text(json.dumps(etags, indent=2) + "\n")
    print("downloaded", changed)
