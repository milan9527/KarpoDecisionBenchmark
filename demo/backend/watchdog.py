"""Stop only configured demo GPU hosts after the durable deadline expires."""
import json
import os
import time

import boto3
from state import Store, claim_stop


def stop_group(store, config, group, now=None, client_factory=boto3.client):
    now = int(time.time()) if now is None else now
    by_region = {}
    for host in config["groups"][group]["hosts"]:
        by_region.setdefault(host["region"], []).append(host["id"])
    # Reconcile late EC2 starts after a prior stop completed. Checking hardware
    # also covers an API crash between StartInstances and its response.
    observed = store.get(group)
    if observed["phase"] == "stopped":
        unexpected = False
        for region, ids in by_region.items():
            ec2 = client_factory("ec2", region_name=region)
            unexpected |= any(h["State"]["Name"] in ("pending", "running", "stopping")
                              for r in ec2.describe_instances(InstanceIds=ids)["Reservations"]
                              for h in r["Instances"])
        if not unexpected:
            return {"group": group, "action": "none"}

        def claim_orphan(old):
            if old["phase"] != "stopped" or old["generation"] != observed["generation"]:
                return None
            old.update(phase="stopping", stop_claim_until=now + 90)
            return old

        claimed = store.update(group, claim_orphan)
    else:
        claimed = store.update(group, lambda old: claim_stop(old, now))
    if claimed is None:
        return {"group": group, "action": "none"}
    errors, states = [], []
    for region, ids in by_region.items():
        try:
            ec2 = client_factory("ec2", region_name=region)
            hosts = [h for r in ec2.describe_instances(InstanceIds=ids)["Reservations"] for h in r["Instances"]]
            states.extend(h["State"]["Name"] for h in hosts)
            running = [h["InstanceId"] for h in hosts if h["State"]["Name"] == "running"]
            if running:
                ec2.stop_instances(InstanceIds=running)
        except Exception as error:
            errors.append(str(error))
    stopped = len(states) == len(config["groups"][group]["hosts"]) and all(s == "stopped" for s in states)

    def finish(old):
        if old["generation"] != claimed["generation"] or old["phase"] != "stopping":
            return None
        old.update(error="; ".join(errors)[:1000], stop_claim_until=now + 45)
        if stopped:
            old.update(phase="stopped", stopped_at=now, hosts=[], ready_gpus=0, stop_after=0)
        return old

    store.update(group, finish)
    return {"group": group, "action": "stopped" if stopped else "stop_requested", "errors": errors}


def handler(event, context):
    config = json.loads(os.environ["DEMO_CONFIG"])
    table = boto3.resource("dynamodb", region_name=config["region"]).Table(os.environ["DEMO_TABLE"])
    store = Store(table)
    results = []
    for group in config["groups"]:
        try:
            results.append(stop_group(store, config, group))
        except Exception as error:
            # A regional/API failure for risk must not prevent H200 cleanup.
            results.append({"group": group, "action": "retry", "error": str(error)[:1000]})
    print(json.dumps(results))
    return results
