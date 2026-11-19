"""Try equivalent single-GPU hosts, creating at most one live instance."""
import hashlib
import json
import pathlib

import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = ROOT / ".state/cloud.json"
CONFIG = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 0})


def main():
    state = json.loads(PATH.read_text())
    if state.get("instance_id"):
        raise RuntimeError("An instance is already journaled")
    user_data = (ROOT / "scripts/bootstrap.sh").read_text().replace(
        "__CODE_SHA__", state["code_sha"]).replace("__MODEL_SHA__", state["model_sha"])
    pricing = boto3.client("pricing", region_name="us-east-1", config=CONFIG)
    for region, location in [("us-east-2", "US East (Ohio)"), ("us-west-2", "US West (Oregon)")]:
        ec2 = boto3.client("ec2", region_name=region, config=CONFIG)
        network = json.loads((ROOT / f".state/alternative-{region}.json").read_text())
        available = json.loads((ROOT / f".state/gpu-alternative-{region}.json").read_text())
        image = available["images"][0]
        sg = next((item["id"] for item in state.get("extra_security_groups", []) if item["region"] == region), None)
        if sg is None:
            sg = ec2.create_security_group(GroupName=state["name"], Description="Isolated Karpo test, no inbound ports",
                                           VpcId=network["subnets"][0]["VpcId"])["GroupId"]
            state.setdefault("extra_security_groups", []).append({"region": region, "id": sg})
            PATH.write_text(json.dumps(state, indent=2))
        for instance_type in ["p5.4xlarge", "g7e.4xlarge", "g7e.8xlarge"]:
            filters = {"instanceType": instance_type, "location": location, "operatingSystem": "Linux",
                       "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used"}
            rates = pricing.get_products(ServiceCode="AmazonEC2", Filters=[
                {"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()], MaxResults=30)
            hourly = None
            for text in rates["PriceList"]:
                obj = json.loads(text)
                for term in obj.get("terms", {}).get("OnDemand", {}).values():
                    for price in term["priceDimensions"].values():
                        if "On Demand" in price["description"] and float(price["pricePerUnit"]["USD"]) > 0:
                            hourly = float(price["pricePerUnit"]["USD"])
                            (ROOT / ".state" / f"price-{region}-{instance_type}.json").write_text(json.dumps(obj, indent=2))
            if hourly is None or hourly > 8.0:
                continue
            for subnet in network["subnets"]:
                if not any(o["InstanceType"] == instance_type and o["Location"] == subnet["AvailabilityZone"]
                           for o in available["offerings"]):
                    continue
                try:
                    result = ec2.run_instances(
                        ImageId=image["id"], InstanceType=instance_type, MinCount=1, MaxCount=1,
                        ClientToken=hashlib.sha256(
                            f"{state['name']}-{instance_type}-{subnet['SubnetId']}".encode()).hexdigest(),
                        IamInstanceProfile={"Name": state["profile"]}, UserData=user_data,
                        NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet["SubnetId"], "Groups": [sg],
                                            "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
                        BlockDeviceMappings=[{"DeviceName": image["root"], "Ebs": {
                            "VolumeSize": 300, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
                        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                        InstanceInitiatedShutdownBehavior="stop",
                        TagSpecifications=[{"ResourceType": kind, "Tags": [
                            {"Key": "Name", "Value": state["name"]},
                            {"Key": "Project", "Value": "KarpoDecisionBenchmark"}]} for kind in ["instance", "volume"]])
                    inst = result["Instances"][0]
                    state.update(region=region, instance_id=inst["InstanceId"], instance_type=instance_type,
                                 hourly_compute_usd=hourly, launch_time=inst["LaunchTime"].isoformat(),
                                 subnet=subnet["SubnetId"], instance_security_group=sg, image=image)
                    state["resources_created"].append("instance")
                    PATH.write_text(json.dumps(state, indent=2))
                    print("LAUNCHED", region, instance_type, inst["InstanceId"], hourly, flush=True)
                    return
                except Exception as exc:
                    state["launch_attempts"].append({"region": region, "instance_type": instance_type,
                                                    "subnet": subnet["SubnetId"], "error": str(exc)})
                    PATH.write_text(json.dumps(state, indent=2))
                    print(region, instance_type, subnet["AvailabilityZone"], str(exc)[:180], flush=True)
                    if "InsufficientInstanceCapacity" not in str(exc):
                        raise
    raise RuntimeError("No capacity in attempted regions/types; project and fixture preparation remain usable.")


if __name__ == "__main__":
    main()
