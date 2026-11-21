"""Provision the demo control plane; records IDs without credentials."""
import io
import json
from pathlib import Path
import secrets
import subprocess
import time
import zipfile

import boto3
from botocore.exceptions import ClientError
from account import new_account, save_credentials

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "demo/config.json").read_text())
NAME, REGION = CONFIG["name"], CONFIG["region"]
OUT = ROOT / "operations/demo-20261121"
STATE = OUT / "resources.json"
ACCOUNT = boto3.client("sts").get_caller_identity()["Account"]
TAGS = [{"Key": "Project", "Value": "KarpoDecisionBenchmark"}, {"Key": "Deployment", "Value": NAME}]


def save(state):
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def existing(call, code="EntityAlreadyExists"):
    try:
        return call()
    except ClientError as error:
        if error.response["Error"]["Code"] != code:
            raise


def propagate(call):
    for attempt in range(10):
        try:
            return call()
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("InvalidParameterValue", "InvalidInstanceID") or attempt == 9:
                raise
            time.sleep(4)


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def allow(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def role(iam, suffix, service, statements, managed=False):
    name = NAME + "-" + suffix
    trust = policy([{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}])
    existing(lambda: iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust, Tags=TAGS))
    iam.put_role_policy(RoleName=name, PolicyName="demo-scoped", PolicyDocument=policy(statements))
    if managed:
        iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    return name, iam.get_role(RoleName=name)["Role"]["Arn"]


def instance_profile(iam, name):
    existing(lambda: iam.create_instance_profile(InstanceProfileName=name, Tags=TAGS))
    if not iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]["Roles"]:
        iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    return name


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"name": NAME, "region": REGION}
    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam")
    s3 = boto3.client("s3", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)
    state["bucket"] = state.get("bucket", NAME + "-" + ACCOUNT)
    if not state.get("bucket_created"):
        s3.create_bucket(Bucket=state["bucket"], CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_public_access_block(Bucket=state["bucket"], PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_ownership_controls(Bucket=state["bucket"], OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
        s3.put_bucket_encryption(Bucket=state["bucket"], ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        state["bucket_created"] = True
        save(state)
    if "node_secret" not in state:
        secret_dir = ROOT / ".state/demo-secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
                        "-subj", "/CN=karpo-demo.internal", "-addext", "subjectAltName=DNS:karpo-demo.internal",
                        "-keyout", str(secret_dir / "node.key"), "-out", str(secret_dir / "node.crt")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        node = {"token": secrets.token_urlsafe(36), "cert": (secret_dir / "node.crt").read_text(),
                "key": (secret_dir / "node.key").read_text()}
        state["node_secret"] = sm.create_secret(Name=NAME + "-node", SecretString=json.dumps(node), Tags=TAGS)["ARN"]
        save(state)
    else:
        node = json.loads(sm.get_secret_value(SecretId=state["node_secret"])["SecretString"])
    if "backend_secret" not in state:
        credentials, account = new_account()
        backend = {"account": account, "cookie_key": secrets.token_urlsafe(48),
                   "node_token": node["token"], "node_cert": node["cert"]}
        state["backend_secret"] = sm.create_secret(Name=NAME + "-backend", SecretString=json.dumps(backend), Tags=TAGS)["ARN"]
        save_credentials(credentials)
        save(state)
    ddb = boto3.client("dynamodb", region_name=REGION)
    state["table"] = NAME
    existing(lambda: ddb.create_table(TableName=NAME, KeySchema=[{"AttributeName": "group", "KeyType": "HASH"}],
                                     AttributeDefinitions=[{"AttributeName": "group", "AttributeType": "S"}],
                                     BillingMode="PAY_PER_REQUEST", Tags=TAGS), "ResourceInUseException")
    table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{NAME}"
    hosts = [h for g in CONFIG["groups"].values() for h in g["hosts"]]
    host_arns = [f"arn:aws:ec2:{h['region']}:{ACCOUNT}:instance/{h['id']}" for h in hosts]
    ssm_arns = [f"arn:aws:ssm:{r}::document/AWS-RunShellScript" for r in {h["region"] for h in hosts}]
    read_instances = allow(["ec2:DescribeInstances", "ec2:DescribeInstanceStatus"], ["*"])
    table_access = allow(["dynamodb:GetItem", "dynamodb:PutItem"], [table_arn])
    backend_name, _ = role(iam, "backend", "ec2.amazonaws.com", [
        read_instances, table_access,
        allow(["ec2:StartInstances"], host_arns),
        allow(["ssm:SendCommand"], host_arns + ssm_arns),
        allow(["ssm:GetCommandInvocation", "ssm:DescribeInstanceInformation"], ["*"]),
        allow(["secretsmanager:GetSecretValue"], [state["backend_secret"]]),
        allow(["s3:GetObject"], [f"arn:aws:s3:::{state['bucket']}/runtime/*"]),
    ], managed=True)
    node_name, _ = role(iam, "node", "ec2.amazonaws.com", [
        allow(["secretsmanager:GetSecretValue"], [state["node_secret"]]),
        allow(["s3:GetObject"], [f"arn:aws:s3:::{state['bucket']}/runtime/*"]),
    ], managed=True)
    state["backend_profile"] = instance_profile(iam, backend_name)
    state["node_profile"] = instance_profile(iam, node_name)
    _, lambda_role = role(iam, "watchdog", "lambda.amazonaws.com", [
        read_instances, table_access, allow(["ec2:StopInstances"], host_arns),
        allow(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
              [f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{NAME}-watchdog:*"]),
    ])
    save(state)
    # Every managed GPU is explicitly allowlisted. Existing non-demo hosts are excluded.
    for region in {h["region"] for h in hosts}:
        regional = boto3.client("ec2", region_name=region)
        for h in [h for h in hosts if h["region"] == region]:
            associations = regional.describe_iam_instance_profile_associations(
                Filters=[{"Name": "instance-id", "Values": [h["id"]]}])["IamInstanceProfileAssociations"]
            if not associations:
                propagate(lambda: regional.associate_iam_instance_profile(
                    InstanceId=h["id"], IamInstanceProfile={"Name": node_name}))
            elif not associations[0]["IamInstanceProfile"]["Arn"].endswith("/" + node_name):
                propagate(lambda: regional.replace_iam_instance_profile_association(
                    AssociationId=associations[0]["AssociationId"], IamInstanceProfile={"Name": node_name}))
    state["vpc"] = "vpc-0d829be5b32cedda7"
    state["subnets"] = ["subnet-08cac12e7a232540b", "subnet-076c65a355de813cf"]
    for kind in ("alb", "backend"):
        key = kind + "_sg"
        if key not in state:
            state[key] = ec2.create_security_group(GroupName=NAME + "-" + kind, Description=NAME,
                VpcId=state["vpc"], TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
            save(state)
    existing(lambda: ec2.authorize_security_group_ingress(GroupId=state["backend_sg"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "UserIdGroupPairs": [{"GroupId": state["alb_sg"]}]}]),
        "InvalidPermission.Duplicate")
    if "backend_instance" not in state:
        ami = boto3.client("ssm", region_name=REGION).get_parameter(
            Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
        user_data = """#!/bin/bash
set -eu
dnf install -y python3.12 python3.12-pip
mkdir -p /opt/karpo-demo
python3.12 -m venv /opt/karpo-demo/venv
/opt/karpo-demo/venv/bin/pip install 'boto3>=1.40,<2' 'fastapi>=0.115,<1' 'httpx>=0.28,<1' 'uvicorn>=0.34,<1'
systemctl enable --now amazon-ssm-agent
"""
        result = ec2.run_instances(ImageId=ami, InstanceType="t4g.small", MinCount=1, MaxCount=1,
            ClientToken=NAME+"-backend", IamInstanceProfile={"Name": backend_name},
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": state["subnets"][0],
                                "AssociatePublicIpAddress": True, "Groups": [state["backend_sg"]]}],
            MetadataOptions={"HttpTokens": "required"}, UserData=user_data,
            BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 16, "VolumeType": "gp3", "Encrypted": True}}],
            TagSpecifications=[{"ResourceType": "instance", "Tags": TAGS+[{"Key": "Name", "Value": NAME+"-backend"}]}])
        state["backend_instance"] = result["Instances"][0]["InstanceId"]
        save(state)
    if "backend_eip" not in state:
        eip = ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": TAGS}])
        state.update(backend_eip=eip["PublicIp"], backend_eip_allocation=eip["AllocationId"])
        save(state)
    propagate(lambda: ec2.associate_address(InstanceId=state["backend_instance"], AllocationId=state["backend_eip_allocation"]))
    for region in {h["region"] for h in hosts}:
        regional = boto3.client("ec2", region_name=region)
        ids = [h["id"] for h in hosts if h["region"] == region]
        instances = [i for r in regional.describe_instances(InstanceIds=ids)["Reservations"] for i in r["Instances"]]
        key = "node_sg_" + region
        if key not in state:
            state[key] = regional.create_security_group(GroupName=NAME+"-node", Description=NAME,
                VpcId=instances[0]["VpcId"], TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
            save(state)
        source = {"UserIdGroupPairs": [{"GroupId": state["backend_sg"]}]} if region == REGION else {
            "IpRanges": [{"CidrIp": state["backend_eip"] + "/32"}]}
        existing(lambda: regional.authorize_security_group_ingress(GroupId=state[key], IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 19443, "ToPort": 19443, **source}]), "InvalidPermission.Duplicate")
        for i in instances:
            groups = [g["GroupId"] for g in i["SecurityGroups"]]
            if state[key] not in groups:
                regional.modify_instance_attribute(InstanceId=i["InstanceId"], Groups=groups+[state[key]])
    elb = boto3.client("elbv2", region_name=REGION)
    if "alb_arn" not in state:
        lb = elb.create_load_balancer(Name=NAME, Subnets=state["subnets"], SecurityGroups=[state["alb_sg"]],
            Scheme="internal", Type="application", IpAddressType="ipv4", Tags=TAGS)["LoadBalancers"][0]
        state.update(alb_arn=lb["LoadBalancerArn"], alb_dns=lb["DNSName"])
        save(state)
    if "target_group" not in state:
        tg = elb.create_target_group(Name=NAME, Protocol="HTTP", Port=8000, VpcId=state["vpc"],
            HealthCheckPath="/healthz", HealthCheckIntervalSeconds=15, HealthyThresholdCount=2,
            TargetType="instance", Tags=TAGS)["TargetGroups"][0]
        state["target_group"] = tg["TargetGroupArn"]
        save(state)
    elb.register_targets(TargetGroupArn=state["target_group"], Targets=[{"Id": state["backend_instance"], "Port": 8000}])
    if "listener" not in state:
        state["listener"] = elb.create_listener(LoadBalancerArn=state["alb_arn"], Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": state["target_group"]}])["Listeners"][0]["ListenerArn"]
        save(state)
    elb.modify_load_balancer_attributes(LoadBalancerArn=state["alb_arn"], Attributes=[
        {"Key": "idle_timeout.timeout_seconds", "Value": "180"}])
    lam = boto3.client("lambda", region_name=REGION)
    code = io.BytesIO()
    with zipfile.ZipFile(code, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in ("state.py", "watchdog.py"):
            archive.write(ROOT / "demo/backend" / file, file)
    function_name = NAME + "-watchdog"
    if "watchdog_arn" not in state:
        for attempt in range(12):
            try:
                state["watchdog_arn"] = lam.create_function(FunctionName=function_name, Runtime="python3.12",
                    Role=lambda_role, Handler="watchdog.handler", Code={"ZipFile": code.getvalue()}, Timeout=60,
                    MemorySize=128, Environment={"Variables": {"DEMO_CONFIG": json.dumps(CONFIG), "DEMO_TABLE": NAME}},
                    Tags={t["Key"]: t["Value"] for t in TAGS})["FunctionArn"]
                save(state)
                break
            except ClientError as error:
                if "cannot be assumed" not in str(error) or attempt == 11:
                    raise
                time.sleep(5)
    else:
        lam.update_function_code(FunctionName=function_name, ZipFile=code.getvalue())
        desired_environment = {"DEMO_CONFIG": json.dumps(CONFIG), "DEMO_TABLE": NAME}
        current = lam.get_function_configuration(FunctionName=function_name)
        if current.get("Environment", {}).get("Variables") != desired_environment:
            lam.get_waiter("function_updated_v2").wait(
                FunctionName=function_name, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
            lam.update_function_configuration(
                FunctionName=function_name, Environment={"Variables": desired_environment})
    events = boto3.client("events", region_name=REGION)
    state["watchdog_rule"] = events.put_rule(Name=NAME+"-stop", ScheduleExpression="rate(1 minute)",
                                            State="ENABLED", Tags=TAGS)["RuleArn"]
    existing(lambda: lam.add_permission(FunctionName=function_name, StatementId="scheduled-stop",
        Action="lambda:InvokeFunction", Principal="events.amazonaws.com", SourceArn=state["watchdog_rule"]),
        "ResourceConflictException")
    events.put_targets(Rule=NAME+"-stop", Targets=[{"Id": "watchdog", "Arn": state["watchdog_arn"]}])
    save(state)
    cf = boto3.client("cloudfront")
    if "vpc_origin" not in state:
        lb = elb.describe_load_balancers(LoadBalancerArns=[state["alb_arn"]])["LoadBalancers"][0]
        if lb["State"]["Code"] != "active":
            print("ALB provisioning; rerun to create CloudFront VPC origin")
            return
        origin = cf.create_vpc_origin(VpcOriginEndpointConfig={"Name": NAME, "Arn": state["alb_arn"],
            "HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only"},
            Tags={"Items": TAGS})["VpcOrigin"]
        state["vpc_origin"] = origin["Id"]
        save(state)
    print(json.dumps(state))


if __name__ == "__main__":
    main()
