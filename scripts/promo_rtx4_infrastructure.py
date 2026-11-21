"""Read-only resource and price snapshot for the current four-RTX topology."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config

OUT = Path(__file__).resolve().parents[1] / "experiments/promo-risk-rtx4-20261121/operations"
CONFIG = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2})


def resources(region, ids):
    client = boto3.client("ec2", region_name=region, config=CONFIG)
    return [
        {"id": i["InstanceId"], "type": i["InstanceType"],
         "az": i["Placement"]["AvailabilityZone"], "state": i["State"]["Name"],
         "region": region, "spot_request": i.get("SpotInstanceRequestId"),
         "private_ip": i.get("PrivateIpAddress")}
        for r in client.describe_instances(InstanceIds=ids)["Reservations"] for i in r["Instances"]
    ]


def prices(instance_type):
    ec2 = boto3.client("ec2", region_name="us-east-2", config=CONFIG)
    spot = ec2.describe_spot_price_history(
        InstanceTypes=[instance_type], AvailabilityZone="us-east-2c",
        ProductDescriptions=["Linux/UNIX"], StartTime=dt.datetime.now(dt.timezone.utc),
    )["SpotPriceHistory"]
    pricing = boto3.client("pricing", region_name="us-east-1", config=CONFIG)
    filters = {"instanceType": instance_type, "location": "US East (Ohio)",
               "operatingSystem": "Linux", "tenancy": "Shared", "preInstalledSw": "NA",
               "capacitystatus": "Used", "marketoption": "OnDemand"}
    response = pricing.get_products(
        ServiceCode="AmazonEC2",
        Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()],
        MaxResults=100,
    )
    assert not response.get("NextToken")
    products = [json.loads(p) for p in response["PriceList"]]
    hourly = {float(d["pricePerUnit"]["USD"]) for p in products
              for t in p["terms"].get("OnDemand", {}).values()
              for d in t["priceDimensions"].values() if d["unit"] == "Hrs"}
    assert len(hourly) == 1 and spot
    return {"type": instance_type, "count": 1 if instance_type == "g7e.12xlarge" else 2,
            "spot_usd_per_hour": float(spot[0]["SpotPrice"]),
            "on_demand_usd_per_hour": hourly.pop(),
            "spot_source": spot, "on_demand_source": products}


def main():
    peers = json.loads((OUT / "small-peers.json").read_text())
    ohio = ["i-0c777e3714538d624", "i-0de4f061c2a04fe4b", "i-05b24e3b4a7ee864d",
            "i-091371593b3c4a5ff", "i-048c3784bb87b6d86"] + [p["instance_id"] for p in peers]
    active = {"i-05b24e3b4a7ee864d": 2, **{p["instance_id"]: 1 for p in peers}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(resources, "us-east-2", ohio),
                 pool.submit(resources, "us-east-1", ["i-05d0c24c739f41ca5"]),
                 pool.submit(prices, "g7e.12xlarge"), pool.submit(prices, "g7e.2xlarge")]
        values = [t.result() for t in tasks]
    instances = values[0] + values[1]
    for i in instances:
        i["participates"] = i["id"] in active
        i["test_gpu_count"] = active.get(i["id"], 0)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    snapshot = {"utc": now, "policy": "Read-only snapshot; instances retained",
                "instances": sorted(instances, key=lambda i: (i["region"], i["id"]))}
    price = {"utc": now, "scope": "Three current RTX instances only; exclude EBS, networking, IPv4 and retained resources",
             "month_hours": 720, "instances": values[2:]}
    for market in ("spot", "on_demand"):
        key = market + "_usd_per_hour"
        price[key] = sum(p[key] * p["count"] for p in values[2:])
    for name, value in (("final-infrastructure.json", snapshot), ("pricing.json", price)):
        (OUT / name).write_text(json.dumps(value, default=str, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"instances": instances, "spot_usd_per_hour": price["spot_usd_per_hour"],
                      "on_demand_usd_per_hour": price["on_demand_usd_per_hour"]}))


if __name__ == "__main__":
    main()
