"""Publish question/answer examples from existing, verifiable GPU responses."""
import copy
from functools import lru_cache
import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RISK = Path("experiments/promo-rtx6-20261121")
SOP = Path("experiments/vllm-sop-h200-20261120")
EXAMPLE_CSS = """
.qa-example{overflow-wrap:anywhere}
.qa-example pre{padding:16px;background:#f4f7fa;border:1px solid #dce5e7;border-radius:8px;
white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 ui-monospace,monospace}
.qa-example details{margin:14px 0}.qa-example summary{cursor:pointer;font-weight:600}
.qa-example .source{font-size:14px;color:#526174}
"""


def read(path):
    return json.loads((ROOT / path).read_text())


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def recorded_row(path, request_id):
    with (ROOT / path).open() as stream:
        rows = [json.loads(line) for line in stream]
    matches = [r for r in rows if r["request_id"] == request_id]
    assert len(matches) == 1
    row = matches[0]
    assert row["valid"] and row["correct"] and row["http_status"] == 200
    return row


def verify_pair(item, row, *, compact=False):
    options = {"ensure_ascii": False}
    options.update({"separators": (",", ":")} if compact else {"sort_keys": True})
    digest = hashlib.sha256(json.dumps(item["request"], **options).encode()).hexdigest()
    assert digest == item["request_sha256"] == row["request_sha256"]
    assert row["expected"] == item["expected"]
    assert abs(row["latency_ms"] - (row["end_offset_s"] - row["scheduled_offset_s"]) * 1000) < .0001


@lru_cache
def examples():
    risk_path = RISK / "results/decision-example.json"
    risk = read(risk_path)
    risk_input = next(r for r in read(RISK / "inputs/review-baseline/risk-requests.json")
                      if r["id"] == risk["request_id"])
    risk_log = RISK / "results/runs/rtx6-calibrated-v1/quality/requests.jsonl"
    risk_row = recorded_row(risk_log, risk["request_id"])
    verify_pair(risk_input, risk_row)
    assert risk["request"] == risk_input["request"]
    assert risk["actual_response"] == risk_row["response"]
    assert risk["latency_ms"] == risk_row["latency_ms"]
    features = risk["request"]["state"]["trusted_features"]
    assert sum(len(group) for group in features.values()) == 200
    decision = risk["actual_response"]["decision"]
    probabilities = risk["actual_response"]["answers"]["action"]["probabilities"]
    assert decision["raw_action"] == "allow" and decision["action"] == "review"
    assert decision["review_pair_score"] == probabilities["review"] / (
        probabilities["review"] + probabilities["allow"])
    assert decision["review_pair_score"] >= decision["threshold"]
    risk_view = {
        "slug": "risk-example",
        "title": "风控问题与回答示例：退款后反复领取权益",
        "scope": "2026-11-21 六 RTX 实际质量阶段的一条合成样例。",
        "question": "用户近30天有10笔订单、8笔退款，退款率80%，退款后又领取权益4次；此次请求应放行还是复核？",
        "question_note": "上句按输入事实整理，便于阅读。实际 user_message 为 “Please process my request.”；模型还收到全部200特征和完整决策规则。",
        "input_note": "输入摘录：仅展示相关特征，实际请求保留全部200特征。",
        "input_excerpt": {"state": {"trusted_features": {
            "orders": {k: features["orders"][k] for k in ("orders_30d", "refunds_30d", "refund_ratio_30d")},
            "promotion": {"claims_after_refund_30d": features["promotion"]["claims_after_refund_30d"]},
        }, "user_message": risk["request"]["state"]["user_message"]}},
        "answer": "原始模型回答 allow（放行），代理校准后的最终回答为 review（复核），与参考动作 review 一致。",
        "answer_note": "实际响应摘录：完整保留 answers 与 decision；usage 和 metrics 可在下方完整响应中查看。",
        "answer_excerpt": {k: risk["actual_response"][k] for k in ("answers", "decision")},
        "explanation": "文档解释：退款次数≥3、退款率≥0.6、退款后领权益次数≥3，满足输入中的复核条件。"
                       f"模型的复核相对分数为{decision['review_pair_score']:.6f}，"
                       f"超过冻结阈值{decision['threshold']:.6f}，因此代理将 allow 升级为 review。"
                       "这段解释由文档整理；模型接口返回动作和概率。",
        "record": risk, "artifact": risk_path.as_posix(),
        "input_source": (RISK / "inputs/review-baseline/risk-requests.json").as_posix(),
        "response_source": risk_log.as_posix(), "sequence": risk_row["sequence"],
    }

    request_id = "concise-en:boundary-duplicate-over-approved-original"
    sop_input = next(r for r in read(SOP / "sop-requests.json") if r["id"] == request_id)
    sop_log = SOP / "results/runs/h200-fp8-v2/confirmation/b4/r8/sop/quality/requests.jsonl"
    sop_row = recorded_row(sop_log, request_id)
    verify_pair(sop_input, sop_row, compact=True)
    state = json.loads(sop_input["request"]["state"])
    criteria = sop_input["request"]["questions"]["action"]["criteria"]
    answer = sop_row["response"]["answers"]["action"]
    assert len(criteria) == 200 and len(answer["probabilities"]) == 200
    assert answer["choice"] == sop_input["expected"]["action"] == "sop_048"
    excerpt = copy.deepcopy(answer)
    excerpt["probabilities"] = dict(sorted(answer["probabilities"].items(), key=lambda x: -x[1])[:3])
    sop = {
        "request_id": request_id, "request": sop_input["request"],
        "request_sha256": sop_input["request_sha256"], "expected": sop_input["expected"],
        "actual_response": sop_row["response"], "latency_ms": sop_row["latency_ms"],
        "source": {"input": (SOP / "sop-requests.json").as_posix(),
                   "response": sop_log.as_posix(), "sequence": sop_row["sequence"],
                   "input_file_sha256": hashlib.sha256((ROOT / SOP / "sop-requests.json").read_bytes()).hexdigest(),
                   "response_file_sha256": hashlib.sha256((ROOT / sop_log).read_bytes()).hexdigest()},
    }
    sop_view = {
        "slug": "sop-example",
        "title": "SOP问题与回答示例：阻止重复提现",
        "scope": "2026-11-20 八 H200 正式确认的质量阶段实际响应，输入来自合成样例。",
        "question": state["customer_message"],
        "question_note": "这是实际客户消息原文。可信状态表明同一幂等键下的操作已经执行。",
        "input_note": "输入 state 的可读形式：原请求中 state 为JSON字符串。实际请求同时携带完整200个SOP候选。",
        "input_excerpt": state,
        "answer": "模型回答 sop_048：防止重复资金操作。同一笔提现已执行，应进入防重复操作步骤。参考动作同为 sop_048。",
        "answer_note": "实际响应摘录：probabilities 仅列最高的3项；完整响应保留全部200项概率。",
        "answer_excerpt": {"answers": {"action": excerpt}},
        "explanation": f"所选候选原文：{criteria['sop_048']}\n\n"
                       "文档解释：execution_exists=true，且幂等键为 probe-executed；"
                       "即使其他审批条件已满足、用户要求重试，也应防止重复执行。"
                       "中文解释是所选候选的释义，接口本身返回候选ID及概率；本次测试没有执行提现。",
        "record": sop, "artifact": "docs/examples/sop-h200-duplicate.json",
        "input_source": (SOP / "sop-requests.json").as_posix(),
        "response_source": sop_log.as_posix(), "sequence": sop_row["sequence"],
    }
    return {"risk": risk_view, "sop": sop_view}


def example_markdown(example, prefix="", full=False):
    record = example["record"]
    result = (
        f'<a id="{example["slug"]}"></a>\n\n## {example["title"]}\n\n{example["scope"]}\n\n'
        f'**问题：** {example["question"]}\n\n{example["question_note"]}\n\n'
        f'{example["input_note"]}\n\n```json\n{pretty(example["input_excerpt"])}\n```\n\n'
        f'**回答：** {example["answer"]}\n\n{example["answer_note"]}\n\n'
        f'```json\n{pretty(example["answer_excerpt"])}\n```\n\n{example["explanation"]}\n\n'
        f'本条端到端耗时 **{record["latency_ms"]:.2f}ms**，'
        f'输入 **{record["actual_response"]["usage"]["input_tokens"]} tokens**。'
        "单条示例用于说明输入输出，性能结论以正式压力测试为准。\n\n"
        f'[完整输入输出 JSON]({prefix}{example["artifact"]}) · '
        f'[原始请求集]({prefix}{example["input_source"]}) · '
        f'[原始响应记录]({prefix}{example["response_source"]})\n\n'
        f'请求ID：`{record["request_id"]}`；响应序号：`{example["sequence"]}`。\n'
    )
    if full:
        for title, value in (("完整请求 JSON（原始结构）", record["request"]),
                             ("完整响应 JSON（原始结构）", record["actual_response"])):
            result += f"\n<details>\n<summary>{title}</summary>\n\n```json\n{pretty(value)}\n```\n\n</details>\n"
    return result


def example_html(example, prefix=""):
    esc = html.escape
    record = example["record"]

    def paragraph(value, strong=False, css=""):
        value = esc(value).replace("\n\n", "<br><br>")
        return f'<p class="{css}">' + (f"<strong>{value}</strong>" if strong else value) + "</p>"

    def code(value):
        return f"<pre><code>{esc(pretty(value))}</code></pre>"

    result = f'<section class="qa-example" id="{example["slug"]}"><h2>{esc(example["title"])}</h2>'
    result += paragraph(example["scope"])
    result += paragraph("问题：" + example["question"], True) + paragraph(example["question_note"])
    result += paragraph(example["input_note"]) + code(example["input_excerpt"])
    result += paragraph("回答：" + example["answer"], True) + paragraph(example["answer_note"])
    result += code(example["answer_excerpt"]) + paragraph(example["explanation"])
    result += paragraph(f'本条端到端耗时 {record["latency_ms"]:.2f}ms，输入 '
                        f'{record["actual_response"]["usage"]["input_tokens"]} tokens。'
                        "单条示例用于说明输入输出，性能结论以正式压力测试为准。")
    for title, value in (("展开完整请求 JSON（原始结构）", record["request"]),
                         ("展开完整响应 JSON（原始结构）", record["actual_response"])):
        result += f"<details><summary>{title}</summary>{code(value)}</details>"
    result += "<p>" + " · ".join(
        f'<a href="{esc(prefix + target, quote=True)}">{label}</a>' for label, target in (
            ("完整输入输出 JSON", example["artifact"]), ("原始请求集", example["input_source"]),
            ("原始响应记录", example["response_source"]))) + "</p>"
    result += paragraph(f'请求ID：{record["request_id"]}；响应序号：{example["sequence"]}。', css="source")
    return result + "</section>"


def main():
    selected = examples()
    sop = selected["sop"]
    artifact = ROOT / sop["artifact"]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(pretty(sop["record"]) + "\n")
    page = "# 风控与SOP：实际模型问题和回答示例\n\n"
    page += "两个示例均来自已完成测试中的实际GPU响应；输入为合成样例。"
    page += "中文业务解释由文档整理，原始模型只返回结构化动作和概率。\n\n"
    page += "\n".join(example_markdown(e, "../", full=True) for e in selected.values())
    page += "\n重建示例：`python scripts/report_examples.py`。该命令仅读取已有测试记录，不调用模型。\n"
    (ROOT / "docs/EXAMPLES.zh.md").write_text(page)
    print("Verified and rendered risk/SOP question-answer examples from recorded responses")


if __name__ == "__main__":
    main()
