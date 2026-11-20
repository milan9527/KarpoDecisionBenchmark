"""Read-only final resource and public-price snapshot for the H200 SOP run."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path

import boto3
from botocore.config import Config

OUT = Path(__file__).resolve().parents[1] / "experiments/vllm-sop-h200-20261120/results"
CONFIG = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 3})


def resources(region, ids):
    client = boto3.client("ec2", region_name=region, config=CONFIG)
    return {
        "region": region,
        "reservations": client.describe_instances(InstanceIds=ids)["Reservations"],
    }


def prices():
    ec2 = boto3.client("ec2", region_name="us-east-2", config=CONFIG)
    spot = ec2.describe_spot_price_history(
        InstanceTypes=["p5en.48xlarge"], AvailabilityZone="us-east-2a",
        ProductDescriptions=["Linux/UNIX"], StartTime=dt.datetime.now(dt.timezone.utc),
    )["SpotPriceHistory"]
    pricing = boto3.client("pricing", region_name="us-east-1", config=CONFIG)
    filters = {
        "instanceType": "p5en.48xlarge", "location": "US East (Ohio)",
        "operatingSystem": "Linux", "tenancy": "Shared", "preInstalledSw": "NA",
        "capacitystatus": "Used", "marketoption": "OnDemand",
    }
    response = pricing.get_products(
        ServiceCode="AmazonEC2",
        Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()],
        MaxResults=100,
    )
    assert not response.get("NextToken"), "Pricing requires pagination"
    products = [json.loads(p) for p in response["PriceList"]]
    hourly = {
        float(d["pricePerUnit"]["USD"])
        for p in products for t in p["terms"].get("OnDemand", {}).values()
        for d in t["priceDimensions"].values() if d["unit"] == "Hrs"
    }
    assert len(hourly) == 1 and spot
    return {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "H200 instance only; exclude retained RTX/H100, EBS, networking and IPv4",
        "spot_usd_per_hour": float(spot[0]["SpotPrice"]),
        "on_demand_usd_per_hour": hourly.pop(),
        "spot_source": spot, "on_demand_source": products, "month_hours": 720,
    }


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(resources, "us-east-2", [
                "i-0c777e3714538d624", "i-0de4f061c2a04fe4b",
                "i-05b24e3b4a7ee864d", "i-091371593b3c4a5ff", "i-048c3784bb87b6d86",
            ]),
            pool.submit(resources, "us-east-1", ["i-05d0c24c739f41ca5"]),
            pool.submit(prices),
        ]
        results = [f.result() for f in futures]
    snapshot = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "policy": "Retain EC2; no shutdown API is used by this script",
        "regions": results[:2],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    for name, value in (("infrastructure-final.json", snapshot), ("pricing-final.json", results[2])):
        (OUT / name).write_text(json.dumps(value, default=str, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "instances": [
            [region["region"], i["InstanceId"], i["InstanceType"], i["State"]["Name"]]
            for region in results[:2] for r in region["reservations"] for i in r["Instances"]
        ],
        "prices": {k: v for k, v in results[2].items() if "source" not in k},
    }))


if __name__ == "__main__":
    main()
