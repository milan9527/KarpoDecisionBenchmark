set -Eeuo pipefail
cat > /opt/karpo/promo-hash-model.py <<'PROMO_HASH'
import datetime, hashlib, json, pathlib, socket
result = {'host': socket.gethostname(), 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'weights': {}}
for path in sorted(pathlib.Path('/opt/karpo/models/karpo-4b').glob('*.safetensors')):
 digest = hashlib.sha256()
 with path.open('rb') as stream:
  for chunk in iter(lambda: stream.read(16*1024*1024), b''): digest.update(chunk)
 result['weights'][path.name] = {'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}
result['ended_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
pathlib.Path('/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/weights-' + socket.gethostname() + '.json').write_text(json.dumps(result, indent=2) + '\n')
PROMO_HASH
systemd-run --unit=karpo-promo-weight-proof-v1 --property=RuntimeMaxSec=900 /usr/bin/python3 /opt/karpo/promo-hash-model.py
