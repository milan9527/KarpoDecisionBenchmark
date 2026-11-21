set -Eeuo pipefail
cat > /opt/karpo/promo-prefetch.py <<'PROMO_PREFETCH'
import concurrent.futures, datetime, json, os, pathlib, socket
started = datetime.datetime.now(datetime.timezone.utc).isoformat()
parts = [(path, start, min(64*1024*1024, path.stat().st_size-start)) for path in pathlib.Path('/opt/karpo/models/karpo-4b').glob('*.safetensors') for start in range(0, path.stat().st_size, 64*1024*1024)]
def read(part):
 path, start, count = part
 with path.open('rb', buffering=0) as stream:
  stream.seek(start)
  while count:
   chunk = stream.read(min(count, 4*1024*1024))
   if not chunk: raise RuntimeError('Unexpected EOF')
   count -= len(chunk)
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
 list(pool.map(read, parts))
result = {'started_utc': started, 'ended_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'parts': len(parts), 'workers': 16, 'scope': 'Read-only OS cache prefetch before pressure'}
pathlib.Path('/opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/prefetch-' + socket.gethostname() + '.json').write_text(json.dumps(result, indent=2) + '\n')
PROMO_PREFETCH
systemd-run --unit=karpo-promo-parallel-prefetch-v1 --property=RuntimeMaxSec=600 /usr/bin/python3 /opt/karpo/promo-prefetch.py
