#!/bin/bash
set -Eeuo pipefail
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/smallbatch3-retry-20261120.tar.gz /tmp/karpo-smallbatch-retry.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 'f5d2315a9dba41c3f95a41e7e8c47595b9b0ad2ae78ece2a2df897a819ea9a84' /tmp/karpo-smallbatch-retry.tar.gz | sha256sum -c -
test ! -e /opt/karpo/experiments/smallbatch3-retry-20261120/manifest.json
tar -xzf /tmp/karpo-smallbatch-retry.tar.gz -C /opt/karpo
mkdir -p /opt/karpo/experiments/smallbatch3-retry-20261120/results/primary
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/results/smallbatch3-retry-20261120/primary/suite-status.json /opt/karpo/experiments/smallbatch3-retry-20261120/results/primary/coordination-preflight.json --region us-east-1 --no-progress
cat > /opt/karpo/experiments/smallbatch3-retry-20261120/launch-primary.sh <<'RUN'
#!/bin/bash
set -Eeuo pipefail
finish() {
 aws s3 sync /opt/karpo/experiments/smallbatch3-retry-20261120/results/primary/ s3://karpo-benchmark-20261119-115003-632930644527/results/smallbatch3-retry-20261120/primary/ --region us-east-1 --no-progress || true
 aws s3 sync /opt/karpo/experiments/smallbatch3-retry-20261120/results/measurements/ s3://karpo-benchmark-20261119-115003-632930644527/results/smallbatch3-retry-20261120/measurements/ --region us-east-1 --no-progress || true
 /sbin/shutdown -h now
}
trap finish EXIT
/opt/karpo/venv/bin/python -u /opt/karpo/scripts/smallbatch3_retry_suite.py --bucket karpo-benchmark-20261119-115003-632930644527 --role primary --secondary-url http://172.31.42.136:18450 >> /opt/karpo/experiments/smallbatch3-retry-20261120/results/primary/launcher.log 2>&1
RUN
systemd-run --unit=karpo-smallbatch-retry-safety --on-active=75m /sbin/shutdown -h now
systemd-run --unit=karpo-smallbatch-retry --property=RuntimeMaxSec=4400 /bin/bash /opt/karpo/experiments/smallbatch3-retry-20261120/launch-primary.sh
