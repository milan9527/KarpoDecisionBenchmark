"""Read-only count/hash reconciliation of every imported historical run."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import sys

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"demo/backend"))
from history import History


def main():
    out = ROOT/"operations/demo-history-20261121"
    manifest = json.loads((out/"import.json").read_text())
    resources = json.loads((ROOT/"operations/demo-20261121/resources.json").read_text())
    sdk = Config(max_pool_connections=12)
    history = History(boto3.resource("dynamodb", region_name=resources["region"], config=sdk).Table(resources["history_table"]),
                      boto3.client("s3", region_name=resources["region"], config=sdk), resources["bucket"])
    entries, cursor = [], None
    while True:
        page = history.listing(cursor)
        entries.extend(page["items"]); cursor = page["cursor"]
        if not cursor:
            break
    imported = {r["id"]: r for r in entries if r["origin"] == "import"}
    assert set(imported) == {r["id"] for r in manifest["records"]}

    def check(expected):
        id_ = expected["id"]
        row = imported[id_]
        assert row["record_count"] == expected["record_count"]
        assert row["source_sha256"] == expected["source_sha256"]
        parts = list(history.all_parts(id_))
        assert sum(p["count"] for p in parts if p["part"][0] == "r") == expected["record_count"]
        assert sum(p["count"] for p in parts if p["part"][0] == "q") == expected["question_count"]
        return len(parts)

    with ThreadPoolExecutor(max_workers=10) as pool:
        counts = list(pool.map(check, manifest["records"]))
    samples = []
    for suffix in (
        "rtx6-calibrated-v1/confirm/summary.json",
        "h200-fp8-v2/confirmation/b4/r8/sop/confirm/summary.json",
        "demo-rps-20261121/sop-100rps-120s-r2.json.gz",
        "demo-rps-20261121-float-diagnostic/risk-100rps-120s-r1.json.gz",
    ):
        row = next(r for r in imported.values() if r["source"].endswith(suffix))
        doc = history.get(row["id"])
        parts = list(history.all_parts(row["id"]))
        for part in (parts[0], parts[-1]):
            raw = gzip.decompress(history.read_part(row["id"], part["part"]))
            assert hashlib.sha256(raw).hexdigest() == part["sha256"]
            assert len(json.loads(raw)) == part["count"]
        if "sop-100rps-120s-r2" in suffix:
            assert doc["metrics"]["failed"] == 2602
            assert doc["metrics"]["passes_performance_acceptance"] is False
        elif "float-diagnostic" in suffix:
            assert doc["metrics"]["passes_performance_acceptance"] is None
        else:
            assert doc["metrics"]["passes_performance_acceptance"] is True
        samples.append({"id": row["id"], "source": row["source"], "sha256_checks": True})
    backup = boto3.client("dynamodb", region_name=resources["region"]).describe_continuous_backups(
        TableName=resources["history_table"])["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"]
    assert backup == "ENABLED"
    result = {"passed": True, "imported_runs": len(imported),
              "response_records": sum(r["record_count"] for r in imported.values()),
              "cloud_parts_checked": sum(counts), "sample_content_checks": samples,
              "point_in_time_recovery": backup,
              "frontend_new_runs": len(entries)-len(imported)}
    (out/"audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
