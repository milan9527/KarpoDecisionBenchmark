set -euo pipefail
TASK_ROOT=/opt/karpo/promo-opt-20261121
mkdir -p "$TASK_ROOT"
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import boto3,hashlib
p='/opt/karpo/promo-opt-runtime-v1.tar.gz'
boto3.client('s3',region_name='us-east-1').download_file('karpo-benchmark-20261119-115003-632930644527','runs/promo-opt-20261121/runtime-v1.tar.gz',p)
assert hashlib.sha256(open(p,'rb').read()).hexdigest()=='7f2de0acfa3418ed21c9f1c3e66d7ad13f33159113f883cab7711fd2f36becfe'
PY
tar -xzf /opt/karpo/promo-opt-runtime-v1.tar.gz -C "$TASK_ROOT"
ln -s /opt/karpo/models "$TASK_ROOT/models"
for TASK_IP in 172.31.42.209 172.31.37.239; do
 scp -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts /opt/karpo/promo-opt-runtime-v1.tar.gz "ubuntu@$TASK_IP:/tmp/promo-opt-runtime-v1.tar.gz"
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts "ubuntu@$TASK_IP" 'sudo /bin/bash -se' <<'PEER'
mkdir -p /opt/karpo/promo-opt-20261121
tar -xzf /tmp/promo-opt-runtime-v1.tar.gz -C /opt/karpo/promo-opt-20261121
ln -s /opt/karpo/models /opt/karpo/promo-opt-20261121/models
PEER
done
systemd-run --unit karpo-opt-environment --property=Type=exec --property=RuntimeMaxSec=7200 --working-directory "$TASK_ROOT" /opt/karpo/venv-vllm034/bin/python -u scripts/promo_opt_controller.py environment
