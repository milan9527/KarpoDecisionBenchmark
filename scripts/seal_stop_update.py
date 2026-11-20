"""Seal documentation and verified stop records without repacking benchmark requests."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

import boto3

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "operations/stop-20261120"
BASE = "ccf3c30b333da9738c801e946b740e5ec200c6bd"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    state = json.loads((OPS / "infrastructure-stopped.json").read_text())
    assert state["all_stopped"]
    assert json.loads((ROOT / "data.json").read_text())["all_project_ec2_stopped"]
    original_rtx = ROOT / "experiments/vllm-rps100-20261120"
    rtx_manifest = json.loads((original_rtx / "evidence-manifest.json").read_text())
    for name, expected in rtx_manifest["files"].items():
        assert sha(original_rtx / name) == expected, name
    original_h200 = ROOT / "experiments/vllm-sop-h200-20261120"
    h200_manifest = json.loads((original_h200 / "evidence-manifest.json").read_text())
    for name, info in h200_manifest["files"].items():
        before = OPS / "previous" / name
        path = before if before.exists() else ROOT / name
        assert sha(path) == info["sha256"], name
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", BASE], text=True, cwd=ROOT).splitlines()
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard"], text=True, cwd=ROOT).splitlines()
    excluded = {str((OPS / name).relative_to(ROOT)) for name in ("evidence-manifest.json", "backup.json")}
    names = sorted(set(changed + untracked) - excluded)
    assert names and all((ROOT / name).is_file() for name in names)
    manifest = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "base_commit": BASE,
        "scope": "User-requested documentation refresh and stop of all project EC2",
        "old_evidence_verified": {"rtx_files": len(rtx_manifest["files"]),
                                  "h200_files": len(h200_manifest["files"]),
                                  "updated_documents_restored_from": "operations/stop-20261120/previous"},
        "files": {name: {"sha256": sha(ROOT / name), "bytes": (ROOT / name).stat().st_size} for name in names},
    }
    target = OPS / "evidence-manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    archive_path = ROOT / ".state/stop-20261120-documents.tar.gz"
    with tarfile.open(archive_path, "w:gz", compresslevel=3) as archive:
        for path in [*(ROOT / name for name in names), target]:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    checksum = sha(archive_path)
    key = f"archives/stop-20261120-documents-{checksum[:16]}.tar.gz"
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.upload_file(str(archive_path), BUCKET, key,
                   ExtraArgs={"ServerSideEncryption": "AES256", "Metadata": {"sha256": checksum}})
    digest, received = hashlib.sha256(), 0
    with s3.get_object(Bucket=BUCKET, Key=key)["Body"] as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
            received += len(chunk)
    assert digest.hexdigest() == checksum and received == archive_path.stat().st_size
    receipt = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "bucket": BUCKET, "key": key,
        "sha256": checksum, "bytes": received, "download_verified": True,
        "files_in_manifest": len(names),
    }
    (OPS / "backup.json").write_text(json.dumps(receipt, indent=2) + "\n")
    # Current publication is separate from the previously sealed benchmark archive.
    for name in [
        "README.md", "RESULTS.zh.md", "report.html", "data.json",
        "operations/stop-20261120/backup.json", "operations/stop-20261120/evidence-manifest.json",
        "operations/stop-20261120/infrastructure-stopped.json",
        "experiments/vllm-sop-h200-20261120/RESULTS.zh.md",
        "experiments/vllm-sop-h200-20261120/report.html",
        "experiments/vllm-sop-h200-20261120/data.json",
    ]:
        s3.upload_file(str(ROOT / name), BUCKET, "publications/stop-20261120/" + name,
                       ExtraArgs={"ServerSideEncryption": "AES256"})
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
