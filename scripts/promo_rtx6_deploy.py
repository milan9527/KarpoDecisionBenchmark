"""Upload frozen runtime and start the scoped controller; redact expiring URLs."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shlex

import boto3

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"
archive = ROOT / ".state/promo-rtx6-v1.tar.gz"
bundle = json.loads((EXP / "bundle-v1.json").read_text())
assert hashlib.sha256(archive.read_bytes()).hexdigest() == bundle["sha256"]
s3 = boto3.client("s3", region_name="us-east-1")
key = "runs/promo-rtx6-20261121/runtime-v1.tar.gz"
s3.upload_file(str(archive), BUCKET, key, ExtraArgs={"Metadata": {"sha256": bundle["sha256"]}})
url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=900)
script = f"""set -euo pipefail
curl --fail --silent --show-error --location {shlex.quote(url)} -o /tmp/promo-rtx6-v1.tar.gz
printf '%s  %s\\n' {bundle['sha256']} /tmp/promo-rtx6-v1.tar.gz | sha256sum -c -
mkdir -p /opt/karpo/promo-rtx6-20261121
tar -xzf /tmp/promo-rtx6-v1.tar.gz -C /opt/karpo/promo-rtx6-20261121
ln -sfn /opt/karpo/models /opt/karpo/promo-rtx6-20261121/models
cat > /opt/karpo/promo-rtx6-20261121/experiments/promo-rtx6-20261121/bundle-v1.json <<'RTX6_BUNDLE'
{json.dumps(bundle, indent=2)}
RTX6_BUNDLE
systemd-run --unit=karpo-rtx6-controller --property=Type=exec --property=RuntimeMaxSec=7200 --working-directory=/opt/karpo/promo-rtx6-20261121 /opt/karpo/venv-vllm034/bin/python -u scripts/promo_rtx6_controller.py
"""
client = boto3.client("ssm", region_name="us-east-2")
result = client.send_command(
    InstanceIds=["i-05b24e3b4a7ee864d"], DocumentName="AWS-RunShellScript",
    Parameters={"commands": ["/bin/bash -se <<'RTX6_DEPLOY'\n"+script+"\nRTX6_DEPLOY"], "executionTimeout": ["120"]})
cid = result["Command"]["CommandId"]
record = {"id": cid, "host": "dual", "region": "us-east-2", "instance": "i-05b24e3b4a7ee864d",
          "target": "i-05b24e3b4a7ee864d", "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
          "script": script.replace(url, "<REDACTED_EXPIRING_DOWNLOAD_URL>"),
          "bucket": BUCKET, "key": key, "sha256": bundle["sha256"]}
path = EXP / "operations/commands" / (cid+".json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(record, indent=2)+"\n")
print(cid)
