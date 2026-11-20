"""Stop the inventoried project instances; retain EBS and record AWS responses."""
import argparse
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "operations/stop-20261120"
CONFIG = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 3})


def write(name, data):
    (OUT / name).write_text(json.dumps(data, default=str, indent=2) + "\n")


def client(region):
    return boto3.client("ec2", region_name=region, config=CONFIG)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["stop", "status"])
    args = parser.parse_args()
    inventory = json.loads((OUT / "inventory-before.json").read_text())
    assert not inventory["errors"]
    if args.action == "stop":
        receipt_path = OUT / "stop-responses.json"
        receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else []
        for region in inventory["regions"]:
            ec2 = client(region["region"])
            ids = [i["InstanceId"] for i in region["instances"]]
            if not ids:
                continue
            current = [i for r in ec2.describe_instances(InstanceIds=ids)["Reservations"]
                       for i in r["Instances"]]
            assert all(any(t["Key"] == "Project" and t["Value"] == "KarpoDecisionBenchmark"
                           for t in i.get("Tags", [])) for i in current)
            assert all(i["RootDeviceType"] == "ebs" for i in current)
            running = [i["InstanceId"] for i in current if i["State"]["Name"] == "running"]
            if running:
                response = ec2.stop_instances(InstanceIds=running)
                receipts.append({"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                 "region": region["region"], "response": response})
                write("stop-responses.json", receipts)
                print(region["region"], response["StoppingInstances"])
    else:
        regions = []
        for region in inventory["regions"]:
            ids = [i["InstanceId"] for i in region["instances"]]
            if not ids:
                continue
            ec2 = client(region["region"])
            current = [i for r in ec2.describe_instances(InstanceIds=ids)["Reservations"]
                       for i in r["Instances"]]
            spot_ids = [i["SpotInstanceRequestId"] for i in current if i.get("SpotInstanceRequestId")]
            spots = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=spot_ids)["SpotInstanceRequests"]
            volumes = ec2.describe_volumes(VolumeIds=[
                b["Ebs"]["VolumeId"] for i in current for b in i["BlockDeviceMappings"] if "Ebs" in b
            ])["Volumes"]
            regions.append({"region": region["region"], "instances": current,
                            "spot_requests": spots, "volumes": volumes})
        instances = [i for r in regions for i in r["instances"]]
        result = {
            "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "authorization": "User requested all project EC2 stop after documentation update",
            "scope": "All KarpoDecisionBenchmark EC2 found across 20 enabled AWS regions",
            "regions": regions, "instance_count": len(instances),
            "all_stopped": bool(instances) and all(i["State"]["Name"] == "stopped" for i in instances),
        }
        write("infrastructure-current.json", result)
        if result["all_stopped"]:
            write("infrastructure-stopped.json", result)
        print(json.dumps({
            "utc": result["utc"], "all_stopped": result["all_stopped"],
            "instances": [[r["region"], i["InstanceId"], i["State"]["Name"]]
                          for r in regions for i in r["instances"]],
        }))


if __name__ == "__main__":
    main()
