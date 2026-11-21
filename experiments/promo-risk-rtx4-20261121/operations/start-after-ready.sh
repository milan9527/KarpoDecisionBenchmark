set -Eeuo pipefail
cat > /opt/karpo/promo-start-after-ready.py <<'PROMO_WAIT_READY'
import datetime, os, time
import httpx
urls = ['http://127.0.0.1:18500', 'http://127.0.0.1:18501', 'http://172.31.42.209:18500', 'http://172.31.37.239:18500']
with httpx.Client(timeout=3, trust_env=False) as client:
 for attempt in range(180):
  try:
   for replica, url in enumerate(urls):
    r = client.get(url + '/healthz')
    r.raise_for_status()
    assert r.json()['replica_id'] == replica
   break
  except (httpx.HTTPError, AssertionError):
   time.sleep(5)
 else:
  raise RuntimeError('Four replicas did not become ready within 15 minutes')
print('All four replicas ready:', datetime.datetime.now(datetime.timezone.utc).isoformat(), flush=True)
python = '/opt/karpo/venv-vllm034/bin/python'
os.execv(python, [python, '-u', '/opt/karpo/promo-validation-v1/scripts/promo_rtx4_suite.py', 'run', '--name', 'rtx4-fp8-v1', '--no-upload', '--urls', *urls])
PROMO_WAIT_READY
systemd-run --unit=karpo-promo-run-v1 --property=RuntimeMaxSec=3600 --property=StandardOutput=append:/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/run-v1.log --property=StandardError=inherit /opt/karpo/venv-vllm034/bin/python -u /opt/karpo/promo-start-after-ready.py
