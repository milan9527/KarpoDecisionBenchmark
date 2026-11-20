#!/bin/bash
set -Eeuo pipefail
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/smallbatch3-20261120.tar.gz /tmp/karpo-smallbatch.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' '773a5556473c551cf7cb4e617bf670461f6272885e72285f7d69da31c69d598d' /tmp/karpo-smallbatch.tar.gz | sha256sum -c -
test ! -e /opt/karpo/experiments/smallbatch3-20261120/manifest.json
tar -xzf /tmp/karpo-smallbatch.tar.gz -C /opt/karpo
mkdir -p /opt/karpo/experiments/smallbatch3-20261120/results/primary
cat > /opt/karpo/experiments/smallbatch3-20261120/launch-primary.sh <<'RUN'
#!/bin/bash
set -Eeuo pipefail
finish() {
 aws s3 sync /opt/karpo/experiments/smallbatch3-20261120/results/primary/ s3://karpo-benchmark-20261119-115003-632930644527/results/smallbatch3-20261120/primary/ --region us-east-1 --no-progress || true
 aws s3 sync /opt/karpo/experiments/smallbatch3-20261120/results/measurements/ s3://karpo-benchmark-20261119-115003-632930644527/results/smallbatch3-20261120/measurements/ --region us-east-1 --no-progress || true
 /sbin/shutdown -h now
}
trap finish EXIT
/opt/karpo/venv/bin/python -u /opt/karpo/scripts/smallbatch3_suite.py --bucket karpo-benchmark-20261119-115003-632930644527 --role primary --secondary-url http://172.31.42.136:18450 >> /opt/karpo/experiments/smallbatch3-20261120/results/primary/launcher.log 2>&1
RUN
systemd-run --unit=karpo-smallbatch-safety --on-active=75m /sbin/shutdown -h now
systemd-run --unit=karpo-smallbatch --property=RuntimeMaxSec=4400 /bin/bash /opt/karpo/experiments/smallbatch3-20261120/launch-primary.sh
