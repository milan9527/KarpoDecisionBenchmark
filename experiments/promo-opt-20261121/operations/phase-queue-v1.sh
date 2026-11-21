set -euo pipefail
cd /opt/karpo/promo-opt-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,time,pathlib
p=pathlib.Path('experiments/promo-opt-20261121/results/controllers/environment.json')
for _ in range(720):
 state=json.loads(p.read_text())
 if state['status']=='complete':break
 if state['status']=='error':raise RuntimeError('Environment phase failed to finish')
 time.sleep(5)
else:raise RuntimeError('Environment phase timeout')
PY
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import urllib.request,hashlib
p='/opt/karpo/promo-opt-review-deploy-v1.tar.gz'
urllib.request.urlretrieve('__PRIVATE_DOWNLOAD_URL__',p)
assert hashlib.sha256(open(p,'rb').read()).hexdigest()=='c7a2f5cfc1260227024658def97a0dab07ed53ca3957f44c123ffd5303b2b5cf'
PY
tar -xzf /opt/karpo/promo-opt-review-deploy-v1.tar.gz
for TASK_IP in 172.31.42.209 172.31.37.239; do
 scp -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts /opt/karpo/promo-opt-review-deploy-v1.tar.gz "ubuntu@$TASK_IP:/tmp/promo-opt-review-deploy-v1.tar.gz"
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts "ubuntu@$TASK_IP" 'sudo tar -xzf /tmp/promo-opt-review-deploy-v1.tar.gz -C /opt/karpo/promo-opt-20261121'
done
/opt/karpo/venv-vllm034/bin/python -u scripts/promo_opt_controller.py performance
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import json,pathlib,hashlib
for name,expected in json.loads(pathlib.Path('experiments/promo-opt-20261121/review-controller-v1.json').read_text())['files'].items():
 assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==expected
PY
/opt/karpo/venv-vllm034/bin/python -u scripts/promo_opt_review_controller.py
