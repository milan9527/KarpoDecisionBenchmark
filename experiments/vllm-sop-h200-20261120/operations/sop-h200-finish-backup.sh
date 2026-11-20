set -Eeuo pipefail
/opt/karpo/venv-vllm034/bin/python - <<'CHECK'
import json
from pathlib import Path
path = Path('/opt/karpo/experiments/vllm-sop-h200-20261120/results/control-mirror/runs/h200-fp8-v2/status.json')
status = json.loads(path.read_text())
assert status['status'] == 'complete' and status['confirmed'] and status['ended_utc']
print('Completed H200 run is mirrored; ending only the temporary backup loop.')
CHECK
systemctl stop karpo-sop-h200-backup.service
systemctl show karpo-sop-h200-backup.service --property=ActiveState,SubState --no-pager
date -u --iso-8601=seconds
