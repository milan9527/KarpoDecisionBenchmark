"""Release only resources in this benchmark's private creation journal."""
import argparse
import datetime as dt
import hashlib
import json
import pathlib

import boto3
from botocore.exceptions import ClientError

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / ".state/cloud.json"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main(action):
    state = json.loads(STATE.read_text())
    if not state["name"].startswith("karpo-benchmark-"):
        raise RuntimeError("Unexpected project journal")
    journal_path = ROOT / "results/cloud-lifecycle.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
        "created_utc": state["created_utc"], "instances": {}, "cleanup": {},
        "cost_scope": "Compute estimate from recorded prices and observed lifecycle, not an AWS invoice.",
    }
    if action != "status" and (
            journal.get("cleanup_paused_by_user") or state.get("lifecycle_policy") == "stop_only"):
        raise RuntimeError(
            "User requested stop only; termination and support-resource deletion are disabled. "
            "Use cloud.py stop for stoppable instances. A new explicit user instruction is "
            "required before changing this lifecycle policy."
        )

    def save():
        journal_path.write_text(json.dumps(journal, indent=2, default=str) + "\n")

    instances = [*state.get("previous_instances", []), state]
    for entry in instances:
        ec2 = boto3.client("ec2", region_name=entry["region"])
        iid = entry["instance_id"]
        instance = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
        if tags.get("Name") not in (state["name"], state["name"] + "-spot") or \
                tags.get("Project") != "KarpoDecisionBenchmark":
            raise RuntimeError(f"Resource ownership does not match: {iid}")
        record = journal["instances"].setdefault(iid, {
            k: entry.get(k) for k in ["instance_type", "region", "launch_time", "hourly_compute_usd", "market"]
        })
        record.update(state=instance["State"]["Name"], observed_utc=now(),
                      state_transition_reason=instance.get("StateTransitionReason"),
                      availability_zone=instance["Placement"]["AvailabilityZone"])
        volume_ids = [m["Ebs"]["VolumeId"] for m in instance.get("BlockDeviceMappings", []) if "Ebs" in m]
        if volume_ids:
            record["volume_ids"] = volume_ids
        if action == "terminate" and record["state"] not in ("terminated", "shutting-down"):
            record["state_before_termination"] = record["state"]
            record["transition_reason_before_termination"] = record["state_transition_reason"]
            record["termination_requested_utc"] = now()
            save()
            ec2.terminate_instances(InstanceIds=[iid])
        save()
    if action != "delete-support":
        print(json.dumps(journal["instances"], indent=2))
        return
    if any(r["state"] != "terminated" for r in journal["instances"].values()):
        raise RuntimeError("Wait for all temporary instances to terminate before deleting support resources")
    for region in sorted({entry["region"] for entry in instances}):
        volumes = boto3.client("ec2", region_name=region).describe_volumes(Filters=[
            {"Name": "tag:Name", "Values": [state["name"], state["name"] + "-spot"]},
            {"Name": "tag:Project", "Values": ["KarpoDecisionBenchmark"]}])["Volumes"]
        if volumes:
            raise RuntimeError(f"Temporary EBS volumes still present in {region}: {[v['VolumeId'] for v in volumes]}")
    journal["cleanup"]["no_remaining_tagged_volumes"] = now()
    save()

    # Verify every remote result against its local copy before deleting the temporary bucket.
    s3 = boto3.client("s3", region_name="us-east-1")
    if not journal["cleanup"].get("bucket_deleted"):
        objects = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=state["bucket"]):
            objects.extend(page.get("Contents", []))
        for item in objects:
            if not item["Key"].startswith("results/"):
                continue
            local = ROOT / item["Key"]
            if not local.is_file():
                raise RuntimeError(f"Missing local evidence: {item['Key']}")
            remote = s3.get_object(Bucket=state["bucket"], Key=item["Key"])["Body"].read()
            if hashlib.sha256(local.read_bytes()).digest() != hashlib.sha256(remote).digest():
                raise RuntimeError(f"Evidence differs: {item['Key']}")
        journal["cleanup"]["verified_s3_objects"] = len(objects)
        save()
        for start in range(0, len(objects), 1000):
            response = s3.delete_objects(Bucket=state["bucket"], Delete={
                "Objects": [{"Key": item["Key"]} for item in objects[start:start + 1000]]})
            if response.get("Errors"):
                raise RuntimeError(response["Errors"])
        s3.delete_bucket(Bucket=state["bucket"])
        journal["cleanup"]["bucket_deleted"] = now()
        save()

    groups = [{"region": "us-east-1", "id": state["security_group"]}, *state.get("extra_security_groups", [])]
    for group in groups:
        if group["id"] in journal["cleanup"]:
            continue
        boto3.client("ec2", region_name=group["region"]).delete_security_group(GroupId=group["id"])
        journal["cleanup"][group["id"]] = now()
        save()
    iam = boto3.client("iam")
    if not journal["cleanup"].get("profile_deleted"):
        try:
            iam.remove_role_from_instance_profile(InstanceProfileName=state["profile"], RoleName=state["role"])
            iam.delete_instance_profile(InstanceProfileName=state["profile"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                raise
        journal["cleanup"]["profile_deleted"] = now()
        save()
    if not journal["cleanup"].get("role_deleted"):
        iam.detach_role_policy(RoleName=state["role"],
                               PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
        iam.delete_role_policy(RoleName=state["role"], PolicyName="benchmark-artifacts")
        iam.delete_role(RoleName=state["role"])
        journal["cleanup"]["role_deleted"] = now()
        save()
    print(json.dumps(journal["cleanup"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "terminate", "delete-support"])
    main(parser.parse_args().action)
