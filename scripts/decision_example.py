"""Present one recorded decision, preserving its complete input and response."""
import html
import json
from pathlib import Path

from prepare import digest
from report_common import table

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/family-20261120"
REQUEST_ID = "concise-en:boundary-duplicate-over-approved-original"
OPTION_IDS = ["sop_048", "sop_081", "sop_017", "sop_106", "sop_162", "sop_063"]
NOTE = "本例来自已完成的合成测试，展示 Karpo 12B FP8 的一次真实记录；不是客户生产数据。"
RATIONALE = (
    "execution_exists=true 表示同一操作已经执行。即使检查通过、审批有效，"
    "也不满足再次执行所要求的“无此前执行”条件。客户要求不能覆盖该规则，"
    "因此参考答案为 sop_048（加密资产提现：防止重复操作），实际选择与其一致。"
)


def load_decision_example():
    fixture = json.loads((EXP / "requests.json").read_text())
    item = next(r for r in fixture if r["id"] == REQUEST_ID)
    response_path = EXP / "results/12b-fp8/requests.jsonl"
    observations = [json.loads(line) for line in response_path.read_text().splitlines()]
    matches = [r for r in observations
               if r["request_id"] == REQUEST_ID and r["stage"] == "quality-boundary"]
    assert len(matches) == 1
    row = matches[0]
    assert row["request_sha256"] == item["request_sha256"] == digest(item["request"])
    assert len(item["request"]["questions"]["action"]["criteria"]) == row["candidate_count"] == 200
    assert row["http_status"] == 200 and row["valid"] and row["correct"]
    assert row["response"]["model"] == "12b-fp8"
    assert row["response"]["answers"]["action"]["choice"] == item["expected"]["action"] == "sop_048"
    return {
        "request_id": REQUEST_ID, "request_sha256": item["request_sha256"],
        "model_label": "Karpo 12B FP8", "candidate_count": 200,
        "request": item["request"], "response": row["response"],
        "expected": item["expected"], "valid": row["valid"], "correct": row["correct"],
        "latency_ms": row["latency_ms"], "http_status": row["http_status"],
        "recorded_utc": row["started_utc"],
        "source": {"request_file": (EXP / "requests.json").relative_to(ROOT).as_posix(),
                   "response_file": response_path.relative_to(ROOT).as_posix(),
                   "sequence": row["sequence"], "stage": row["stage"]},
        "displayed_option_ids": OPTION_IDS,
    }


def answer_fields(example):
    action = example["response"]["answers"]["action"]
    return {"action": {key: action[key] for key in ["type", "choice", "confidence"]}}


def decision_example_markdown(example, artifact_link):
    state = json.loads(example["request"]["state"])
    question = example["request"]["questions"]["action"]
    options = "\n".join(
        f'| {key} | {question["criteria"][key].replace("|", chr(92) + "|")} |'
        for key in OPTION_IDS)
    return f"""{NOTE}

1. **业务问题与输入**：客户说“{state['customer_message']}”。业务域为加密资产提现，完整状态如下：

```json
{json.dumps(state, ensure_ascii=False, indent=2)}
```

2. **模型选项**：一次请求传入完整200项。下表摘出6项帮助理解；仅报告摘录，程序未筛选候选。英文为实际输入原文。

| 候选 ID | 实际描述 |
|---|---|
{options}

实际决策指令：

```text
{question['instructions']}
```

3. **实际回答**：{example['model_label']} 选择 **sop_048**，参考答案也是 **sop_048**。本次完整响应耗时 **{example['latency_ms']:.1f} ms**，为单次记录。响应关键字段如下：

```json
{json.dumps(answer_fields(example), ensure_ascii=False, indent=2)}
```

**判定说明（报告解读）**：{RATIONALE} 模型返回选择及评分，没有生成这段解释。

[完整请求与响应 JSON（含全部200项及评分）]({artifact_link})
"""


def decision_example_html(example):
    esc = html.escape
    state = json.loads(example["request"]["state"])
    question = example["request"]["questions"]["action"]
    options = question["criteria"]
    excerpt = table(["候选 ID", "实际候选描述（英文原文）"],
                    [[key, options[key]] for key in OPTION_IDS], "本例候选摘录")
    full = table(["候选 ID", "实际候选描述（英文原文）"],
                 list(map(list, options.items())), "本例完整200候选")
    return f"""<section id="decision-example">
<div class="section-head"><h2>完整示例：问题 → 200候选 → 模型回答</h2></div>
<div class="panel panel-padding text-block">
<p>{esc(NOTE)}</p>
<h3>1 · 业务问题与输入事实</h3>
<p>业务域：加密资产提现。需要决定此刻应走哪个 SOP。</p>
<blockquote>{esc(state['customer_message'])}</blockquote>
<details open><summary>实际输入状态（完整）</summary>
<pre class="example-code">{esc(json.dumps(state, ensure_ascii=False, indent=2))}</pre></details>
<details><summary>实际决策指令（英文原文）</summary>
<p>{esc(question['instructions'])}</p></details>
<h3>2 · 传入完整200项，由模型选择一项</h3>
<p>下面摘出6项帮助理解，包括同域的不同动作和其他业务域的候选。仅报告摘录，程序未筛选候选。</p>
<div class="example-options">{excerpt}</div>
<details id="all-example-options"><summary>展开本次实际传入的全部200项</summary>
<div class="example-options candidate-catalog">{full}</div></details>
<h3>3 · 模型实际回答</h3>
<p><strong>{esc(example['model_label'])} → <span id="example-choice">sop_048</span> · 防止重复操作</strong></p>
<p>参考答案：sop_048，判定正确。本次完整响应耗时 <strong id="example-latency">{example['latency_ms']:.1f} ms</strong>（单次记录）。</p>
<p>响应关键字段：</p>
<pre class="example-code" id="example-answer">{esc(json.dumps(answer_fields(example), ensure_ascii=False, indent=2))}</pre>
<p><strong>判定说明（报告解读）：</strong>{esc(RATIONALE)}</p>
<p>模型返回选择及评分，没有生成上述解释；confidence 字段不能当作业务准确率。</p>
<p><a href="@@DECISION@@">完整请求与响应 JSON（含全部200项及评分）</a></p>
</div></section>"""
