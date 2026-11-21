"""Connect the private S3 frontend and internal ALB to CloudFront."""
import json
import mimetypes
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "operations/demo-20261121/resources.json"


def save(state):
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def main():
    state = json.loads(STATE.read_text())
    cf = boto3.client("cloudfront")
    ec2 = boto3.client("ec2", region_name=state["region"])
    origin = cf.get_vpc_origin(Id=state["vpc_origin"])["VpcOrigin"]
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [state["vpc"]]},
        {"Name": "group-name", "Values": ["CloudFront-VPCOrigins-Service-SG"]}])["SecurityGroups"]
    if groups:
        from botocore.exceptions import ClientError
        try:
            ec2.authorize_security_group_ingress(GroupId=state["alb_sg"], IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                 "UserIdGroupPairs": [{"GroupId": groups[0]["GroupId"]}]}])
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise
    if origin["Status"] != "Deployed":
        print("CloudFront VPC origin status:", origin["Status"])
        return
    if "oac" not in state:
        state["oac"] = cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": state["name"], "Description": "Private demo frontend bucket",
            "SigningProtocol": "sigv4", "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]["Id"]
        save(state)
    if "headers_policy" not in state:
        state["headers_policy"] = cf.create_response_headers_policy(ResponseHeadersPolicyConfig={
            "Name": state["name"], "SecurityHeadersConfig": {
                "ContentTypeOptions": {"Override": True},
                "FrameOptions": {"FrameOption": "DENY", "Override": True},
                "ReferrerPolicy": {"ReferrerPolicy": "same-origin", "Override": True},
                "StrictTransportSecurity": {"AccessControlMaxAgeSec": 31536000, "IncludeSubdomains": True, "Override": True},
                "ContentSecurityPolicy": {"ContentSecurityPolicy":
                    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                    "Override": True}}})["ResponseHeadersPolicy"]["Id"]
        save(state)
    if "distribution" not in state:
        distribution = cf.create_distribution(DistributionConfig={
            "CallerReference": state["name"], "Comment": "Karpo live risk and SOP demonstration",
            "Enabled": True, "DefaultRootObject": "index.html", "PriceClass": "PriceClass_100",
            "Origins": {"Quantity": 2, "Items": [
                {"Id": "frontend", "DomainName": f"{state['bucket']}.s3.{state['region']}.amazonaws.com",
                 "OriginPath": "/site", "S3OriginConfig": {"OriginAccessIdentity": ""},
                 "OriginAccessControlId": state["oac"]},
                {"Id": "backend", "DomainName": state["alb_dns"],
                 "VpcOriginConfig": {"VpcOriginId": state["vpc_origin"],
                                     "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "frontend", "ViewerProtocolPolicy": "redirect-to-https",
                "Compress": True, "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
                "ResponseHeadersPolicyId": state["headers_policy"]},
            "CacheBehaviors": {"Quantity": 1, "Items": [
                {"PathPattern": "/api/*", "TargetOriginId": "backend", "ViewerProtocolPolicy": "https-only",
                 "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
                                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
                 "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                 "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3",
                 "ResponseHeadersPolicyId": state["headers_policy"], "Compress": True}]},
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
            "HttpVersion": "http2and3", "IsIPV6Enabled": True,
        })["Distribution"]
        state.update(distribution=distribution["Id"], domain=distribution["DomainName"],
                     distribution_arn=distribution["ARN"])
        save(state)
    s3 = boto3.client("s3", region_name=state["region"])
    s3.put_bucket_policy(Bucket=state["bucket"], Policy=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Sid": "CloudFrontOACReadSiteOnly", "Effect": "Allow",
             "Principal": {"Service": "cloudfront.amazonaws.com"}, "Action": "s3:GetObject",
             "Resource": f"arn:aws:s3:::{state['bucket']}/site/*",
             "Condition": {"StringEquals": {"AWS:SourceArn": state["distribution_arn"]}}},
            {"Sid": "RequireTLS", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
             "Resource": [f"arn:aws:s3:::{state['bucket']}", f"arn:aws:s3:::{state['bucket']}/*"],
             "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        ]}))
    for path in (ROOT / "demo/frontend").rglob("*"):
        if path.is_file():
            key = "site/" + path.relative_to(ROOT / "demo/frontend").as_posix()
            s3.upload_file(str(path), state["bucket"], key, ExtraArgs={
                "ContentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "CacheControl": "no-cache" if path.suffix == ".html" else "public,max-age=300"})
    cf.create_invalidation(DistributionId=state["distribution"], InvalidationBatch={
        "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": __import__("uuid").uuid4().hex})
    print("Demo URL: https://" + state["domain"])
    print("Distribution:", cf.get_distribution(Id=state["distribution"])["Distribution"]["Status"])


if __name__ == "__main__":
    main()
