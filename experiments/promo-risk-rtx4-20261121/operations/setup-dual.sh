set -Eeuo pipefail
PROMO_ROOT=/opt/karpo/promo-validation-v1
PROMO_PY=/opt/karpo/venv-vllm034/bin/python
mkdir -p "$PROMO_ROOT"
aws s3 cp s3://karpo-benchmark-20261119-115003-632930644527/inputs/promo-risk-rtx4-v1.tar.gz /tmp/promo-risk-rtx4-v1.tar.gz --region us-east-1 --no-progress
printf '%s  %s\n' 'dd0b37780b7e34fe2f1630466784ffbd5a29e48cf3c9a1f6c1e6d091a503e52b' /tmp/promo-risk-rtx4-v1.tar.gz | sha256sum -c -
tar -xzf /tmp/promo-risk-rtx4-v1.tar.gz -C "$PROMO_ROOT"
ln -s /opt/karpo/models "$PROMO_ROOT/models"
cd "$PROMO_ROOT"
mkdir -p experiments/promo-risk-rtx4-20261121/results/setup
"$PROMO_PY" -m pip install --no-deps --no-build-isolation ./serving/vllm_karpo > experiments/promo-risk-rtx4-20261121/results/setup/dual-adapter-install.log 2>&1
"$PROMO_PY" -m unittest discover -s tests -p test_promo_risk.py > experiments/promo-risk-rtx4-20261121/results/setup/dual-promo-tests.log 2>&1
"$PROMO_PY" -m unittest discover -s tests -p test_vllm_rps.py > experiments/promo-risk-rtx4-20261121/results/setup/dual-rps-tests.log 2>&1
PYTHONPATH="$PROMO_ROOT/tests" "$PROMO_PY" -m unittest test_vllm_karpo.MarkerPoolingTests > experiments/promo-risk-rtx4-20261121/results/setup/dual-adapter-tests.log 2>&1
"$PROMO_PY" scripts/promo_rtx4_suite.py encode
"$PROMO_PY" scripts/promo_rtx4_suite.py sync
install -d -m 700 /opt/karpo/.ssh
if [ ! -f /opt/karpo/.ssh/promo-rtx21 ]; then ssh-keygen -q -t ed25519 -N '' -f /opt/karpo/.ssh/promo-rtx21; fi
cat /opt/karpo/.ssh/promo-rtx21.pub
systemd-run --unit=karpo-promo-dual-v1 --property=RuntimeMaxSec=10800 --property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/dual-node-launch.log --property=StandardError=inherit "$PROMO_PY" -u "$PROMO_ROOT/scripts/promo_rtx4_suite.py" node --name dual-v1 --replica-ids 0 1 --bind 0.0.0.0
