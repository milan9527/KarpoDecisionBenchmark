"""Estimate compute cost from lifecycle timestamps and historical Spot quotes."""
import datetime as dt
import json
import pathlib
import re

import boto3

ROOT = pathlib.Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


def main():
    path = ROOT / "results/cloud-lifecycle.json"
    journal = json.loads(path.read_text())
    total = 0
    for iid, record in journal["instances"].items():
        if record["state"] not in ("shutting-down", "terminated"):
            raise RuntimeError(f"Instance has no confirmed termination transition: {iid}")
        start = dt.datetime.fromisoformat(record["launch_time"])
        end = dt.datetime.fromisoformat(record["termination_requested_utc"])
        reason = record.get("transition_reason_before_termination") or ""
        stopped = re.search(r"\((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) GMT\)", reason)
        if stopped and record.get("state_before_termination") in ("stopped", "stopping"):
            end = dt.datetime.strptime(stopped.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        initial_rate = record["hourly_compute_usd"]
        points = [(start, initial_rate)]
        if record.get("market") == "spot":
            ec2 = boto3.client("ec2", region_name=record["region"])
            history = []
            for page in ec2.get_paginator("describe_spot_price_history").paginate(
                    InstanceTypes=[record["instance_type"]], AvailabilityZone=record["availability_zone"],
                    ProductDescriptions=["Linux/UNIX"], StartTime=start, EndTime=end):
                history.extend(page["SpotPriceHistory"])
            record["spot_history"] = [{"timestamp": h["Timestamp"].isoformat(), "usd_per_hour": float(h["SpotPrice"])}
                                      for h in sorted(history, key=lambda x: x["Timestamp"])]
            for item in record["spot_history"]:
                timestamp = dt.datetime.fromisoformat(item["timestamp"])
                if timestamp <= start:
                    points[0] = (start, item["usd_per_hour"])
                elif timestamp < end:
                    points.append((timestamp, item["usd_per_hour"]))
        points.sort()
        segments = []
        cost = 0
        for index, (begin, rate) in enumerate(points):
            finish = points[index + 1][0] if index + 1 < len(points) else end
            seconds = (finish - begin).total_seconds()
            cost += seconds * rate / 3600
            segments.append({"start": begin.isoformat(), "end": finish.isoformat(),
                             "seconds": seconds, "usd_per_hour": rate})
        record.update(estimated_compute_usd=round(cost, 6), estimated_seconds=(end - start).total_seconds(),
                      price_segments=segments)
        total += cost
    journal["estimated_total_compute_usd"] = round(total, 6)
    journal["estimate_notes"] = [
        "Launch-to-stop/termination-request interval includes provisioning time; not an AWS invoice.",
        "Spot historical quotes integrated over the interval; actual billing adjustments are not included.",
        "Excludes EBS, snapshots, public IPv4, S3 requests/storage, and any network charges.",
    ]
    path.write_text(json.dumps(journal, indent=2) + "\n")
    print(json.dumps({"estimated_compute_usd": round(total, 4),
                      "instances": {k: {"seconds": r["estimated_seconds"], "usd": r["estimated_compute_usd"]}
                                    for k, r in journal["instances"].items()}}, indent=2))


if __name__ == "__main__":
    main()
