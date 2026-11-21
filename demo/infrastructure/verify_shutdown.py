"""Observe the real shutdown deadline without renewing any demo lease.

Read-only AWS verification. Does not invoke Lambda, shorten deadlines or stop
instances itself; the deployed EventBridge/Lambda must complete the shutdown.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import boto3

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-20261121"


def main():
    resources = json.loads((OUT / "resources.json").read_text())
    config = json.loads((ROOT / "demo/config.json").read_text())
    validation = json.loads((OUT / "browser-validation.json").read_text())
    deadlines = {g: v["stop_after"] for g, v in validation["after_end"]["groups"].items()}
    table = boto3.resource("dynamodb", region_name=config["region"]).Table(resources["table"])
    clients = {region: boto3.client("ec2", region_name=region)
               for region in {h["region"] for s in config["groups"].values() for h in s["hosts"]}}
    proof = {"deadline_seconds": 600, "deadlines": deadlines,
             "observer": "read-only DescribeInstances/GetItem; no stop or Lambda invocation",
             "samples": []}
    existing = OUT / "shutdown-validation.json"
    if existing.exists():
        recorded = json.loads(existing.read_text())
        if recorded.get("deadlines") == deadlines:
            proof = recorded
    first_stop = {}
    for sample in proof["samples"]:
        for group, value in sample["groups"].items():
            if any(state in ("stopping", "stopped") for state in value["states"].values()):
                first_stop.setdefault(group, sample["at"])
    # The AWS stop API is asynchronous. Preserve the original 600s deadline
    # and allow time to observe final EC2 state propagation after guest poweroff.
    timeout = max(deadlines.values()) + 900
    previous = None
    while time.time() < timeout:
        now = int(time.time())
        sample = {"at": now, "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(), "groups": {}}
        for group, spec in config["groups"].items():
            item = table.get_item(Key={"group": group}, ConsistentRead=True)["Item"]
            states = {}
            for region in {h["region"] for h in spec["hosts"]}:
                ids = [h["id"] for h in spec["hosts"] if h["region"] == region]
                result = clients[region].describe_instances(InstanceIds=ids)
                states.update({h["InstanceId"]: h["State"]["Name"]
                               for r in result["Reservations"] for h in r["Instances"]})
            sample["groups"][group] = {"phase": item["phase"], "states": states,
                                      "stop_after": int(item["stop_after"])}
            if any(state in ("stopping", "stopped") for state in states.values()):
                assert now >= deadlines[group], "GPU stopped before the requested ten-minute deadline"
                first_stop.setdefault(group, now)
        proof["samples"].append(sample)
        summary = {g: (v["phase"], sorted(set(v["states"].values()))) for g, v in sample["groups"].items()}
        if summary != previous:
            print(sample["utc"], summary, flush=True)
            previous = summary
        (OUT / "shutdown-validation.json").write_text(json.dumps(proof, indent=2) + "\n")
        if all(v["phase"] == "stopped" and all(s == "stopped" for s in v["states"].values())
               for v in sample["groups"].values()):
            break
        time.sleep(10)
    else:
        raise AssertionError("Automatic shutdown did not complete within the verification window")
    cpu = clients[config["region"]].describe_instances(InstanceIds=[resources["backend_instance"]])
    cpu_state = cpu["Reservations"][0]["Instances"][0]["State"]["Name"]
    assert cpu_state == "running"
    proof.update(completed_at=datetime.now(timezone.utc).isoformat(),
                 first_observed_stopping=first_stop,
                 first_observed_lag_seconds={g: t-deadlines[g] for g, t in first_stop.items()},
                 backend_state=cpu_state, passed=True)
    logs = boto3.client("logs", region_name=config["region"])
    proof["watchdog_logs"] = [
        {"timestamp": event["timestamp"], "message": event["message"]}
        for event in logs.filter_log_events(logGroupName="/aws/lambda/" + resources["name"] + "-watchdog",
            startTime=(min(deadlines.values())-60)*1000)["events"]
    ]
    (OUT / "shutdown-validation.json").write_text(json.dumps(proof, indent=2) + "\n")
    print("All configured GPU EC2 stopped automatically; CPU API remains running.", flush=True)


if __name__ == "__main__":
    main()
