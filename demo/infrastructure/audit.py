"""Read-only checks for the deployed demo's private origins and stop allowlist."""
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request

import boto3

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-20261121"


def http_status(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def main():
    resources = json.loads((OUT / "resources.json").read_text())
    config = json.loads((ROOT / "demo/config.json").read_text())
    region = resources["region"]
    account = boto3.client("sts").get_caller_identity()["Account"]
    s3 = boto3.client("s3", region_name=region)
    block = s3.get_public_access_block(Bucket=resources["bucket"])["PublicAccessBlockConfiguration"]
    assert all(block.values())
    policy = json.loads(s3.get_bucket_policy(Bucket=resources["bucket"])["Policy"])
    grants = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
    assert len(grants) == 1
    assert grants[0]["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert grants[0]["Resource"] == f"arn:aws:s3:::{resources['bucket']}/site/*"
    assert grants[0]["Condition"]["StringEquals"]["AWS:SourceArn"] == resources["distribution_arn"]
    cf = boto3.client("cloudfront")
    distribution = cf.get_distribution(Id=resources["distribution"])["Distribution"]
    origins = {o["Id"]: o for o in distribution["DistributionConfig"]["Origins"]["Items"]}
    assert origins["frontend"]["OriginAccessControlId"] == resources["oac"]
    oac = cf.get_origin_access_control(Id=resources["oac"])["OriginAccessControl"]["OriginAccessControlConfig"]
    assert oac["SigningBehavior"] == "always" and oac["SigningProtocol"] == "sigv4"
    assert origins["backend"]["VpcOriginConfig"]["VpcOriginId"] == resources["vpc_origin"]
    elb = boto3.client("elbv2", region_name=region)
    alb = elb.describe_load_balancers(LoadBalancerArns=[resources["alb_arn"]])["LoadBalancers"][0]
    assert alb["Scheme"] == "internal"
    targets = elb.describe_target_health(TargetGroupArn=resources["target_group"])["TargetHealthDescriptions"]
    assert len(targets) == 1 and targets[0]["TargetHealth"]["State"] == "healthy"
    ec2 = boto3.client("ec2", region_name=region)
    sg = ec2.describe_security_groups(GroupIds=[resources["alb_sg"]])["SecurityGroups"][0]
    assert len(sg["IpPermissions"]) == 1
    ingress = sg["IpPermissions"][0]
    assert ingress["FromPort"] == 80 and ingress["ToPort"] == 80
    assert not ingress.get("IpRanges") and not ingress.get("Ipv6Ranges")
    assert len(ingress["UserIdGroupPairs"]) == 1
    source_group = ec2.describe_security_groups(GroupIds=[
        ingress["UserIdGroupPairs"][0]["GroupId"]])["SecurityGroups"][0]
    assert source_group["GroupName"] == "CloudFront-VPCOrigins-Service-SG"
    role_name = resources["name"] + "-watchdog"
    iam = boto3.client("iam")
    statements = []
    for policy_name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        statements += iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)["PolicyDocument"]["Statement"]
    allowed = {f"arn:aws:ec2:{h['region']}:{account}:instance/{h['id']}"
               for spec in config["groups"].values() for h in spec["hosts"]}
    stop_grants = [s for s in statements if "ec2:StopInstances" in s["Action"]]
    assert len(stop_grants) == 1 and set(stop_grants[0]["Resource"]) == allowed
    assert not iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
    events = boto3.client("events", region_name=region)
    rule = events.describe_rule(Name=resources["name"] + "-stop")
    assert rule["State"] == "ENABLED" and rule["ScheduleExpression"] == "rate(1 minute)"
    target = events.list_targets_by_rule(Rule=resources["name"] + "-stop")["Targets"]
    assert len(target) == 1 and target[0]["Arn"] == resources["watchdog_arn"]
    statuses = {
        "cloudfront_frontend": http_status("https://" + resources["domain"]),
        "anonymous_api": http_status("https://" + resources["domain"] + "/api/status"),
        "anonymous_s3": http_status(f"https://{resources['bucket']}.s3.{region}.amazonaws.com/site/index.html"),
    }
    assert statuses == {"cloudfront_frontend": 200, "anonymous_api": 401, "anonymous_s3": 403}, statuses
    report = {
        "at": datetime.now(timezone.utc).isoformat(), "passed": True, "http": statuses,
        "s3_public_access_block": block, "s3_allow": grants[0], "oac": oac,
        "distribution_status": distribution["Status"], "alb_scheme": alb["Scheme"],
        "alb_health": targets, "alb_ingress": ingress,
        "watchdog_stop_resource_allowlist": sorted(allowed),
        "watchdog_schedule": rule["ScheduleExpression"],
        "watchdog_target": target[0]["Arn"],
    }
    (OUT / "security-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Private S3/OAC, internal ALB, login, and six-instance stop allowlist verified.")


if __name__ == "__main__":
    main()
