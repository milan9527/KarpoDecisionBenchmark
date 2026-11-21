set -eu
/opt/karpo/venv-vllm034/bin/python - <<'PY'
from pathlib import Path
p=Path('/opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results')
for f in sorted(p.glob('nodes/*/replica-*/server.log'),key=lambda f:f.stat().st_mtime)[-2:]:
 print(f.parent)
 print('\n'.join(f.read_text(errors='replace').splitlines()[-7:]))
PY
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv
