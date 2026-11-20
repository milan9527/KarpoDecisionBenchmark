"""Hash the completed H200 evidence and verify a private S3 archive by rereading it."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import tarfile

import boto3

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-sop-h200-20261120"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    status = json.loads((EXP / "results/runs/h200-fp8-v2/status.json").read_text())
    verification = json.loads((EXP / "results/verification.json").read_text())
    assert status["status"] == "complete" and status["confirmed"] and status.get("ended_utc")
    assert verification["confirmed"] and verification["verified_stages"] == len(status["stages"])
    paths = [
        p for p in EXP.rglob("*") if p.is_file()
        and p.name not in ("evidence-manifest.json", "backup.json")
        and "__pycache__" not in p.parts
    ]
    paths += [ROOT / "README.md", *sorted((ROOT / "scripts").glob("sop_h200_*.py"))]
    assert not any(".state" in p.relative_to(ROOT).parts for p in paths)
    manifest = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "files": {str(p.relative_to(ROOT)): {"bytes": p.stat().st_size, "sha256": sha(p)}
                  for p in sorted(paths)},
        "excludes": ["evidence-manifest.json (self)", "backup.json (archive receipt)"],
    }
    target = EXP / "evidence-manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    paths.append(target)
    archive_path = ROOT / ".state/sop-h200-final.tar.gz"
    with tarfile.open(archive_path, "w:gz", compresslevel=3) as archive:
        for path in sorted(paths):
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    archive_sha = sha(archive_path)
    key = f"archives/sop-h200-20261120-final-{archive_sha[:16]}.tar.gz"
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.upload_file(str(archive_path), BUCKET, key, ExtraArgs={
        "ServerSideEncryption": "AES256", "Metadata": {"sha256": archive_sha},
    })
    response = s3.get_object(Bucket=BUCKET, Key=key)
    remote_sha = hashlib.sha256()
    received = 0
    with response["Body"] as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            received += len(chunk)
            remote_sha.update(chunk)
    assert remote_sha.hexdigest() == archive_sha and received == archive_path.stat().st_size
    backup = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "bucket": BUCKET, "key": key,
        "bytes": received, "sha256": archive_sha, "download_verified": True,
        "files_in_manifest": len(manifest["files"]),
        "scope": "New H200 experiment and current README/H200 scripts; old experiments are not modified",
    }
    (EXP / "backup.json").write_text(json.dumps(backup, indent=2) + "\n")
    for path in (
        EXP / "backup.json", target, EXP / "RESULTS.zh.md", EXP / "report.html", EXP / "data.json",
    ):
        s3.upload_file(str(path), BUCKET, f"publications/{EXP.name}/{path.name}",
                       ExtraArgs={"ServerSideEncryption": "AES256"})
    print(json.dumps(backup))


if __name__ == "__main__":
    main()
