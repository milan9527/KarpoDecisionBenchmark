"""Seal the completed local evidence and verify a private S3 archive round trip."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

import boto3

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    verification = json.loads((EXP / "results/verification.json").read_text())
    assert verification["verified_requests"] > 0
    final = json.loads((EXP / "results/runs/rtx6-calibrated-v1/status.json").read_text())
    assert final["status"] == "complete"
    archive = ROOT / ".state/promo-rtx6-complete-20261121.tar.gz"
    if archive.exists():
        raise RuntimeError("Archive already exists; preserve this sealed run")
    excluded = {EXP / "EVIDENCE.json", EXP / "operations/archive-receipt.json"}
    files = {
        p for directory in (EXP,)
        for p in directory.rglob("*") if p.is_file() and p not in excluded and "__pycache__" not in p.parts
    }
    frozen = json.loads((EXP / "manifest-v1.json").read_text())
    files.update(ROOT / p for p in frozen["files"])
    files.update((ROOT / "scripts").glob("promo_rtx6*.py"))
    files.update((ROOT / "tests").glob("test_promo_rtx6*.py"))
    files.update(ROOT / p for p in [
        "README.md", "RESULTS.zh.md", "data.json", "report.html", "scripts/latest_report.py",
        "scripts/report.py", "scripts/project_report.py", "scripts/promo_rtx4_infrastructure.py",
        "tests/test_promo_opt_calibration.py", "tests/test_promo_opt_calibrated_proxy.py",
        "experiments/promo-opt-20261121/inputs/review-baseline/risk-requests.json",
        "experiments/promo-opt-20261121/results/verification.json",
        "experiments/promo-opt-20261121/operations/final-infrastructure.json",
        "experiments/promo-opt-20261121/operations/archive-receipt.json",
    ])
    evidence = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "verified_requests": verification["verified_requests"], "accepted": final["accepted"],
        "resource_policy": "User explicitly requested retaining EC2 after the test; five RTX instances remain running",
        "files": {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)},
    }
    manifest = EXP / "EVIDENCE.json"
    manifest.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    files.add(manifest)
    with tarfile.open(archive, "w:gz") as output:
        for path in sorted(files):
            output.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    digest = sha(archive)
    key = "archives/promo-rtx6-20261121/complete-v1.tar.gz"
    client = boto3.client("s3", region_name="us-east-1")
    client.upload_file(str(archive), BUCKET, key, ExtraArgs={"Metadata": {"sha256": digest}})
    head = client.head_object(Bucket=BUCKET, Key=key)
    assert head["ContentLength"] == archive.stat().st_size
    assert head["Metadata"]["sha256"] == digest
    downloaded = archive.with_name("promo-rtx6-archive-roundtrip.tar.gz")
    client.download_file(BUCKET, key, str(downloaded))
    assert sha(downloaded) == digest
    downloaded.unlink()
    receipt = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "bucket": BUCKET, "key": key, "bytes": archive.stat().st_size,
               "sha256": digest, "files": len(files), "download_hash_verified": True}
    (EXP / "operations/archive-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
