set -eu
cd /opt/karpo/promo-rtx6-20261121
/opt/karpo/venv-vllm034/bin/python - <<'PY'
import concurrent.futures,json,sys
sys.path.insert(0,'scripts')
from promo_rtx6_controller import command
from promo_rtx6_runtime import EXP
fleet=json.loads((EXP/'fleet-v1.json').read_text())
def check(h):
 text=f"systemctl show karpo-rtx6-model-{h['name']} --property=ActiveState,SubState,ExecMainStatus\n"
 text+=f"cat {EXP}/results/nodes/{h['name']}/status.json\n"
 text+=f"tail -n 3 {EXP}/results/nodes/{h['name']}/replica-*/server.log\n"
 try:return h['name'],command(h,text,30)
 except Exception as error:return h['name'],str(error)
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
 for name,result in pool.map(check,fleet['hosts']):print(name,result)
PY
