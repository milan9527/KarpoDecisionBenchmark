"""Read-only final resource and current-price audit for the six-RTX experiment."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path

import boto3
from promo_rtx4_infrastructure import resources

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"


def prices(region, zone, instance_type, count):
    spot = boto3.client("ec2", region_name=region).describe_spot_price_history(
        InstanceTypes=[instance_type], AvailabilityZone=zone, ProductDescriptions=["Linux/UNIX"],
        StartTime=dt.datetime.now(dt.timezone.utc))["SpotPriceHistory"]
    filters = {"instanceType": instance_type, "regionCode": region, "operatingSystem": "Linux",
               "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used", "marketoption": "OnDemand"}
    result = boto3.client("pricing", region_name="us-east-1").get_products(ServiceCode="AmazonEC2",
        Filters=[{"Type":"TERM_MATCH", "Field":k, "Value":v} for k,v in filters.items()], MaxResults=100)
    assert not result.get("NextToken")
    products = [json.loads(p) for p in result["PriceList"]]
    hourly = {float(d["pricePerUnit"]["USD"]) for p in products for t in p["terms"].get("OnDemand", {}).values()
              for d in t["priceDimensions"].values() if d["unit"] == "Hrs"}
    assert len(hourly) == 1 and spot
    return {"region": region, "az": zone, "type": instance_type, "count": count,
            "spot_usd_per_hour": float(spot[0]["SpotPrice"]), "on_demand_usd_per_hour": hourly.pop(),
            "spot_source": spot, "on_demand_source": products}


def main():
    previous = json.loads((ROOT / "experiments/promo-opt-20261121/operations/final-infrastructure.json").read_text())
    fleet = json.loads((EXP / "fleet-v1.json").read_text())
    active = {h["instance_id"]: h for h in fleet["hosts"]}
    ids = {r["id"]: r["region"] for r in previous["instances"]}
    ids.update({h["instance_id"]: h["region"] for h in fleet["hosts"]})
    groups = Counter((h["region"],h["az"],h["instance_type"]) for h in fleet["hosts"])
    with ThreadPoolExecutor(max_workers=5) as pool:
        res = [pool.submit(resources, region, [i for i,r in ids.items() if r==region]) for region in sorted(set(ids.values()))]
        quotes = [pool.submit(prices, *key, count) for key,count in groups.items()]
        instances = [row for job in res for row in job.result()]
        price_rows = [job.result() for job in quotes]
    for row in instances:
        row["participates"] = row["id"] in active
        row["test_gpu_count"] = len(active[row["id"]]["replica_ids"]) if row["participates"] else 0
        assert row["state"] == ("running" if row["participates"] else "stopped"), row
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    snapshot = {"utc": now, "policy": "Read-only audit; all participating EC2 retained running",
                "instances": sorted(instances, key=lambda r:(r["region"],r["id"]))}
    price = {"utc": now, "instances": price_rows, "month_hours": 720,
             "scope": "Five participating Spot instances; excludes EBS, public IPv4, cross-region traffic and other stopped resources"}
    for market in ("spot","on_demand"):
        price[market+"_usd_per_hour"] = sum(p[market+"_usd_per_hour"]*p["count"] for p in price_rows)
    for name,value in (("final-infrastructure.json",snapshot),("pricing.json",price)):
        (EXP / "operations" / name).write_text(json.dumps(value,default=str,indent=2)+"\n")
    print(json.dumps({"running":sum(i["state"]=="running" for i in instances),
                      "gpus":sum(i["test_gpu_count"] for i in instances),
                      "spot_usd_per_hour":price["spot_usd_per_hour"]}))


if __name__ == "__main__":
    main()
