"""Dedicated benchmark resources. Commands are run via SSM, with no inbound ports."""
import argparse
import datetime as dt
import json
import pathlib
import time

import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / ".state/cloud.json"
REGION = "us-east-1"
CONFIG = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2})


def client(name):
    region = json.loads(STATE.read_text()).get("region", REGION) if STATE.exists() else REGION
    return boto3.client(name, region_name=region, config=CONFIG)


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, default=str) + "\n")


def load():
    return json.loads(STATE.read_text())


def launch():
    if STATE.exists():
        raise RuntimeError("Existing resource journal: inspect it before creating another instance.")
    identity = client("sts").get_caller_identity()
    suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = "karpo-benchmark-" + suffix
    state = {"name": name, "region": REGION, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
             "lifecycle_policy": "stop_only",
             "instance_type": "p5.4xlarge", "hourly_compute_usd": 6.88,
             "bucket": f"{name}-{identity['Account']}", "role": name, "profile": name,
             "vpc": "vpc-0c036c30b82b3703f", "resources_created": []}
    save(state)
    s3, iam, ec2 = client("s3"), client("iam"), client("ec2")
    s3.create_bucket(Bucket=state["bucket"])
    state["resources_created"].append("bucket")
    save(state)
    s3.put_public_access_block(Bucket=state["bucket"], PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=state["bucket"], ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                              "Action": "sts:AssumeRole"}]}))["Role"]["Arn"]
    state["resources_created"].append("role")
    save(state)
    iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    iam.put_role_policy(RoleName=name, PolicyName="benchmark-artifacts", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{state['bucket']}/inputs/*"},
            {"Effect": "Allow", "Action": "s3:PutObject", "Resource": f"arn:aws:s3:::{state['bucket']}/results/*"}]}))
    iam.create_instance_profile(InstanceProfileName=name)
    state["resources_created"].append("profile")
    save(state)
    iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    sg = ec2.create_security_group(GroupName=name, Description="Karpo benchmark: SSM, no inbound access",
                                   VpcId=state["vpc"])["GroupId"]
    state["security_group"] = sg
    state["resources_created"].append("security_group")
    save(state)
    meta = json.loads((ROOT / ".state/discovery-karpo_meta.json").read_text())
    state["code_sha"], state["model_sha"] = meta["code"]["sha"], meta["model"]["sha"]
    image_id = "ami-032f90794e9cc5df4"
    image = ec2.describe_images(ImageIds=[image_id])["Images"][0]
    state["image"] = {"id": image_id, "name": image["Name"]}
    script = (ROOT / "scripts/bootstrap.sh").read_text().replace("__CODE_SHA__", state["code_sha"]).replace(
        "__MODEL_SHA__", state["model_sha"])
    subnets = ["subnet-0693778771c7fe672", "subnet-029c0264a7e188a46",
               "subnet-035e3e2820bda44e3", "subnet-0280c6306405e4dfa", "subnet-062a148d1354577b3"]
    state["launch_attempts"] = []
    save(state)
    # Allow the new instance profile to propagate. Each wait is short.
    time.sleep(10)
    for subnet in subnets:
        try:
            response = ec2.run_instances(
                ImageId=image_id, InstanceType="p5.4xlarge", MinCount=1, MaxCount=1,
                ClientToken=f"{name}-{subnet}",
                IamInstanceProfile={"Name": name}, UserData=script,
                NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet, "Groups": [sg],
                                    "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
                BlockDeviceMappings=[{"DeviceName": image["RootDeviceName"],
                                      "Ebs": {"VolumeSize": 300, "VolumeType": "gp3",
                                              "Encrypted": True, "DeleteOnTermination": True}}],
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                InstanceInitiatedShutdownBehavior="stop",
                TagSpecifications=[{"ResourceType": t, "Tags": [
                    {"Key": "Name", "Value": name}, {"Key": "Project", "Value": "KarpoDecisionBenchmark"},
                    {"Key": "Purpose", "Value": "temporary-inference-benchmark"}]} for t in ["instance", "volume"]])
            instance = response["Instances"][0]
            state.update(instance_id=instance["InstanceId"], subnet=subnet, launch_time=instance["LaunchTime"].isoformat())
            state["resources_created"].append("instance")
            save(state)
            print(json.dumps({k: state[k] for k in ["instance_id", "instance_type", "subnet", "bucket", "launch_time"]}))
            return
        except Exception as exc:
            state["launch_attempts"].append({"subnet": subnet, "error": str(exc)})
            save(state)
            if "InsufficientInstanceCapacity" not in str(exc):
                raise
    raise RuntimeError("No p5.4xlarge capacity in attempted subnets; see resource journal.")


def status():
    state = load()
    response = client("ec2").describe_instances(InstanceIds=[state["instance_id"]])
    instance = response["Reservations"][0]["Instances"][0]
    managed = client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [state["instance_id"]]}])["InstanceInformationList"]
    print(json.dumps({"instance_id": state["instance_id"], "state": instance["State"]["Name"],
                      "private_ip": instance.get("PrivateIpAddress"),
                      "ssm": [{k: m.get(k) for k in ["PingStatus", "PlatformName", "AgentVersion"]} for m in managed]}))


def command(script, timeout):
    state = load()
    result = client("ssm").send_command(
        InstanceIds=[state["instance_id"]], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
        TimeoutSeconds=60, Comment="Karpo isolated benchmark")
    command_id = result["Command"]["CommandId"]
    state.setdefault("commands", []).append({"id": command_id, "region": state["region"],
                                            "instance_id": state["instance_id"],
                                            "sent_utc": dt.datetime.now(dt.timezone.utc).isoformat()})
    save(state)
    print(command_id)


def output(command_id):
    r = client("ssm").get_command_invocation(CommandId=command_id, InstanceId=load()["instance_id"])
    print(json.dumps({k: r.get(k) for k in ["Status", "ResponseCode", "ExecutionElapsedTime"]}))
    print(r.get("StandardOutputContent", ""))
    print(r.get("StandardErrorContent", ""))


def upload(path, key):
    client("s3").upload_file(str(path), load()["bucket"], "inputs/" + key)
    print("Uploaded", key)


def fetch():
    state = load()
    s3 = client("s3")
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=state["bucket"], Prefix="results/"):
        for item in page.get("Contents", []):
            dest = ROOT / item["Key"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(state["bucket"], item["Key"], str(dest))
            files.append(item["Key"])
    print(json.dumps(files))


def stop():
    """Stop only. Unsupported one-time Spot requests must fail, never terminate."""
    state = load()
    response = client("ec2").stop_instances(InstanceIds=[state["instance_id"]])
    changed = response["StoppingInstances"]
    state["stop_requested_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save(state)
    print(json.dumps(changed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ["launch", "status", "fetch", "stop"]:
        sub.add_parser(name)
    p = sub.add_parser("command")
    p.add_argument("--script", type=pathlib.Path, required=True)
    p.add_argument("--timeout", type=int, default=600)
    sub.add_parser("output").add_argument("command_id")
    p = sub.add_parser("upload")
    p.add_argument("path", type=pathlib.Path)
    p.add_argument("key")
    args = parser.parse_args()
    if args.action == "command":
        command(args.script.read_text(), args.timeout)
    elif args.action == "output":
        output(args.command_id)
    elif args.action == "upload":
        upload(args.path, args.key)
    else:
        globals()[args.action]()
