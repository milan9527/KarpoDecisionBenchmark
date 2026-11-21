set -Eeuo pipefail
/opt/karpo/venv-vllm034/bin/python - <<'SMOKE'
import json,time,sys
from pathlib import Path
import httpx
root=Path('/opt/karpo/promo-validation-v1');sys.path.insert(0,str(root/'scripts'))
from benchmark import validate
exp=root/'experiments/promo-risk-rtx4-20261121'
items=json.loads((exp/'risk-requests.json').read_text())
enc={r['request_id']:r for r in json.loads((exp/'encoding-reference.json').read_text())}
result=[]
for behavior in ['normal','identity_reuse','refund_arbitrage','reward_pooling']:
 item=next(i for i in items if i['group']=='tune' and i['order']=='original' and i['behavior']==behavior)
 start=time.perf_counter();r=httpx.post('http://127.0.0.1:18500/v1/systemone',json=item['request'],timeout=120,trust_env=False);elapsed=(time.perf_counter()-start)*1000
 r.raise_for_status();body=r.json();correct=validate(body,item)
 assert body['metrics']['tokens_sha256']==enc[item['id']]['tokens_sha256']
 assert body['metrics']['marker']==enc[item['id']]['marker']
 result.append({'request_id':item['id'],'expected':item['expected'],'correct':correct,'response':body,'latency_ms':elapsed,'scope':'single-replica smoke on tune cases, not the four-GPU acceptance run'})
 print(behavior,item['expected'],body['answers']['action']['choice'],correct,round(elapsed,2),flush=True)
(exp/'results/setup/smoke.json').write_text(json.dumps(result,indent=2))
SMOKE
/opt/karpo/venv-vllm034/bin/python /opt/karpo/promo-validation-v1/scripts/promo_rtx4_suite.py sync
