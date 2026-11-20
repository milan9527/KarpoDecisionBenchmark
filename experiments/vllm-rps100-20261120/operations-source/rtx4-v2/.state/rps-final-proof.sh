set -Eeuo pipefail
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import datetime as dt,json,subprocess
from pathlib import Path
result={
 'utc':dt.datetime.now(dt.timezone.utc).isoformat(),
 'hostname':subprocess.check_output(['hostname'],text=True).strip(),
 'scheduled_shutdown_present':Path('/run/systemd/shutdown/scheduled').exists(),
 'timers':subprocess.check_output(['systemctl','list-timers','--all','--no-pager'],text=True),
 'karpo_units':subprocess.check_output(['systemctl','list-units','--all','--no-pager','karpo*'],text=True),
 'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=index,name,memory.used,utilization.gpu','--format=csv'],text=True),
}
assert not result['scheduled_shutdown_present']
print(json.dumps(result))
PY
