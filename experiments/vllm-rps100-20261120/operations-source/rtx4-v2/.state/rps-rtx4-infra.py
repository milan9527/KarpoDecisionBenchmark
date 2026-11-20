"""Capture current benchmark instances and regional Spot reference costs."""
import datetime as dt
import json
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/vllm-rps100-20261120/results"
peer = json.loads((OUT / "fleet-instances.json").read_text())[0]
groups = {
    "us-east-2": ["i-0de4f061c2a04fe4b", "i-05b24e3b4a7ee864d",
                  "i-091371593b3c4a5ff", "i-048c3784bb87b6d86"],
    peer["region"]: [peer["InstanceId"]],
}
rows = []
for region, ids in groups.items():
    ec2 = boto3.client("ec2", region_name=region)
    instances = [i for r in ec2.describe_instances(InstanceIds=ids)["Reservations"] for i in r["Instances"]]
    prices = ec2.describe_spot_price_history(
        InstanceTypes=sorted({i["InstanceType"] for i in instances}),
        ProductDescriptions=["Linux/UNIX"], StartTime=dt.datetime.now(dt.timezone.utc),
        MaxResults=100)["SpotPriceHistory"]
    rates = {(p["InstanceType"], p["AvailabilityZone"]): float(p["SpotPrice"]) for p in prices}
    for i in instances:
        row = {
            "id": i["InstanceId"], "type": i["InstanceType"], "state": i["State"]["Name"],
            "region": region, "private_ip": i["PrivateIpAddress"],
            "az": i["Placement"]["AvailabilityZone"], "market": i.get("InstanceLifecycle", "on-demand"),
            "launch_time": str(i["LaunchTime"]),
            "in_rtx4_benchmark": i["InstanceId"] != "i-048c3784bb87b6d86" and i["InstanceType"].startswith("g7e."),
        }
        if row["state"] == "running":
            assert row["market"] == "spot"
            row["reference_compute_usd_per_hour"] = rates[row["type"], row["az"]]
        rows.append(row)
record = {
    "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "ec2_policy": "keep_running", "instances": rows,
    "running_instances": sum(r["state"] == "running" for r in rows),
    "reference_compute_usd_per_hour": sum(r.get("reference_compute_usd_per_hour", 0) for r in rows),
    "rtx4_reference_compute_usd_per_hour": sum(r.get("reference_compute_usd_per_hour", 0)
                                             for r in rows if r["in_rtx4_benchmark"]),
    "excludes": "EBS, network, public IPv4 and price changes",
    "old_stopped_instance": "i-048c3784bb87b6d86 was stopped before this optimization; not stopped by this task",
}
assert record["running_instances"] == 4, record
assert sum(r["in_rtx4_benchmark"] for r in rows) == 3
(OUT / "infrastructure-current.json").write_text(json.dumps(record, indent=2) + "\n")
(OUT / "rtx4/infrastructure.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record))
