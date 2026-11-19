"""Try persistent, stoppable Spot capacity and stop the On-Demand fallback on success."""
import hashlib
import json
import pathlib

import boto3
from botocore.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = ROOT / ".state/cloud.json"
CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 0})


def main():
    state = json.loads(PATH.read_text())
    if state.get("market") == "spot":
        raise RuntimeError("Spot instance already exists")
    prices = json.loads((ROOT / ".state/spot-prices.json").read_text())
    candidates = []
    for region, entries in prices.items():
        for item in entries:
            already_tried = any(a["instance_type"] == item["instance_type"] and a["az"] == item["az"]
                                and a["region"] == region for a in state.get("spot_attempts", []))
            if item["instance_type"] in ["g7e.4xlarge", "g7e.8xlarge", "p5.4xlarge"] and not already_tried:
                candidates.append({**item, "region": region})
    candidates.sort(key=lambda x: (x["region"] != "us-east-1", x["usd_per_hour"]))
    script = (ROOT / "scripts/bootstrap.sh").read_text().replace(
        "__CODE_SHA__", state["code_sha"]).replace("__MODEL_SHA__", state["model_sha"])
    for item in candidates:
        region = item["region"]
        if region == "us-east-1":
            net = json.loads((ROOT / ".state/discovery-network.json").read_text())
            subnet = next((x for x in net["subnets"] if x["VpcId"] == state["vpc"]
                           and x["AvailabilityZone"] == item["az"] and x.get("DefaultForAz")), None)
            image = state["image"]["id"]
            sg = state["security_group"]
        else:
            net = json.loads((ROOT / f".state/alternative-{region}.json").read_text())
            subnet = next((x for x in net["subnets"] if x["AvailabilityZone"] == item["az"]), None)
            image = json.loads((ROOT / f".state/gpu-alternative-{region}.json").read_text())["images"][0]["id"]
            sg = next(x["id"] for x in state["extra_security_groups"] if x["region"] == region)
        if not subnet:
            continue
        ec2 = boto3.client("ec2", region_name=region, config=CONFIG)
        cap = 3.99 if item["instance_type"].startswith("g7e") else 6.88
        if item["usd_per_hour"] > cap:
            continue
        try:
            result = ec2.run_instances(
                ImageId=image, InstanceType=item["instance_type"], MinCount=1, MaxCount=1,
                ClientToken=hashlib.sha256(f"{state['name']}-persistent-spot-{region}-{item['az']}-{item['instance_type']}".encode()).hexdigest(),
                InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {
                    "MaxPrice": str(cap), "SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop"}},
                InstanceInitiatedShutdownBehavior="stop",
                IamInstanceProfile={"Name": state["profile"]}, UserData=script,
                NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet["SubnetId"], "Groups": [sg],
                                    "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
                BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                    "VolumeSize": 300, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                TagSpecifications=[{"ResourceType": kind, "Tags": [
                    {"Key": "Name", "Value": state["name"] + "-spot"},
                    {"Key": "Project", "Value": "KarpoDecisionBenchmark"}]} for kind in ["instance", "volume"]])
            instance = result["Instances"][0]
            previous = {k: state.get(k) for k in ["instance_id", "instance_type", "region", "launch_time", "hourly_compute_usd"]}
            state.setdefault("previous_instances", []).append(previous)
            state.update(instance_id=instance["InstanceId"], instance_type=item["instance_type"], region=region,
                         market="spot", spot_type="persistent", lifecycle_policy="stop_only",
                         launch_time=instance["LaunchTime"].isoformat(),
                         hourly_compute_usd=item["usd_per_hour"], spot_price_cap=cap,
                         spot_request_id=instance.get("SpotInstanceRequestId"), subnet=subnet["SubnetId"],
                         active_image_id=image)
            PATH.write_text(json.dumps(state, indent=2))
            print("SPOT LAUNCHED", state["instance_id"], region, item["instance_type"], item["usd_per_hour"], flush=True)
            if previous.get("instance_id"):
                boto3.client("ec2", region_name=previous["region"], config=CONFIG).stop_instances(
                    InstanceIds=[previous["instance_id"]])
                print("STOPPED FALLBACK", previous["instance_id"], flush=True)
            return
        except Exception as exc:
            state.setdefault("spot_attempts", []).append({**item, "error": str(exc)})
            PATH.write_text(json.dumps(state, indent=2))
            print("SPOT", region, item["az"], item["instance_type"], str(exc)[:180], flush=True)
            if "InsufficientInstanceCapacity" not in str(exc) and "UnfulfillableCapacity" not in str(exc):
                raise
    print("No Spot capacity; keeping current fallback.")


if __name__ == "__main__":
    main()
