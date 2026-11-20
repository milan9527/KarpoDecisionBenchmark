"""Publish the current risk/SOP conclusions and the verified project stop state."""
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "operations/stop-20261120"
H200 = ROOT / "experiments/vllm-sop-h200-20261120"
RTX = ROOT / "experiments/vllm-rps100-20261120"


def load(path):
    return json.loads(path.read_text())


def table(headers, rows):
    return '<div class="table"><table><thead><tr>' + "".join(
        f"<th>{html.escape(str(x))}</th>" for x in headers
    ) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in row) + "</tr>" for row in rows
    ) + "</tbody></table></div>"


def md_table(headers, rows):
    return "\n".join(
        ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
        + ["| " + " | ".join(map(str, row)) + " |" for row in rows]
    )


def main():
    state = load(OPS / "infrastructure-stopped.json")
    assert state["all_stopped"]
    instances = [
        {"region": r["region"], "id": i["InstanceId"], "type": i["InstanceType"],
         "az": i["Placement"]["AvailabilityZone"], "state": i["State"]["Name"]}
        for r in state["regions"] for i in r["instances"]
    ]
    assert len(instances) == state["instance_count"] and all(i["state"] == "stopped" for i in instances)
    risk_data, sop_data = load(RTX / "data.json"), load(H200 / "data.json")
    risk_rounds = [r for r in risk_data["performance"]
                   if r["run"] == "rtx4-fp8-v1" and r["scenario"] == "risk"
                   and r["stage"] in ("confirm", "repeat")]
    sop_rounds = [r["summary"] | {"stage": r["stage"]} for r in sop_data["stages"]
                  if r["phase"] == "confirmation" and r["stage"] in ("confirm", "repeat")]
    risk_quality = next(r for r in risk_data["quality"]
                        if r["run"] == "rtx4-fp8-v1" and r["scenario"] == "risk")
    sop_quality = next(r["summary"] for r in sop_data["stages"]
                       if r["phase"] == "confirmation" and r["stage"] == "quality")
    assert len(risk_rounds) == len(sop_rounds) == 2
    assert all(r["passes_performance_acceptance"] and r["failures"] == 0
               and r["correct"] / r["attempts"] > .9 for r in risk_rounds + sop_rounds)
    assert risk_quality["accuracy"] > .9 and sop_quality["accuracy"] > .9
    prices = [("风控 · 四RTX", load(OPS / "risk-pricing.json")),
              ("SOP · 八H200", load(H200 / "results/pricing-final.json"))]
    rows, compact = [], []
    for scenario, rounds, quality in [
        ("风控 · 四RTX", risk_rounds, risk_quality), ("SOP · 八H200", sop_rounds, sop_quality)
    ]:
        for r in rounds:
            rows.append([
                scenario, "第一轮" if r["stage"] == "confirm" else "第二轮", f"{r['steady_valid_rps']:.2f}",
                f"{r['p50_ms']:.2f}", f"{r['p95_ms']:.2f}", f"{r['valid']:,}/{r['attempts']:,}",
                f"{r['correct']/r['attempts']:.2%}", r["failures"],
            ])
            compact.append({k: r[k] for k in (
                "stage", "target_rps", "offered_seconds", "attempts", "valid", "correct",
                "failures", "steady_valid_rps", "p50_ms", "p95_ms"
            )} | {"scenario": scenario})
    headers = ["场景", "120秒确认", "稳态RPS", "P50 ms", "P95 ms", "有效/全部", "压力正确率", "失败"]
    quality_rows = [
        ["风控", f"{risk_quality['correct']}/{risk_quality['attempts']}", f"{risk_quality['accuracy']:.2%}",
         "独立quality集；压力循环tune集"],
        ["SOP", f"{sop_quality['correct']}/{sop_quality['attempts']}", f"{sop_quality['accuracy']:.2%}",
         "质量和压力复用相同104条样例"],
    ]
    q_headers = ["场景", "合成质量", "正确率", "样本边界"]
    cost_headers = ["方案", "Spot美元/小时", "Spot美元/月", "按需美元/小时", "按需美元/月"]
    cost_rows = [[name, f"{p['spot_usd_per_hour']:.4f}", f"{p['spot_usd_per_hour']*720:,.2f}",
                  f"{p['on_demand_usd_per_hour']:.4f}", f"{p['on_demand_usd_per_hour']*720:,.2f}"]
                 for name, p in prices]
    i_headers = ["区域 / 可用区", "实例", "型号", "当前状态"]
    i_rows = [[i["az"], i["id"], i["type"], i["state"]] for i in instances]
    gpu_rows = []
    for r in sop_data["stages"]:
        if r["phase"] == "confirmation" and r["stage"] in ("confirm", "repeat"):
            samples = r["gpu_steady_samples"]
            gpu_rows.append(["第一轮" if r["stage"] == "confirm" else "第二轮",
                             f"{sum(x['utilization_mean_pct'] for x in samples)/len(samples):.2f}%",
                             f"{min(x['utilization_mean_pct'] for x in samples):.1f}–"
                             f"{max(x['utilization_mean_pct'] for x in samples):.1f}%"])
    g_headers = ["H200正式轮次", "八卡平均利用率", "各卡均值范围"]
    scope = (
        "100 RPS表示每秒固定到达100个请求，不是持续100个请求在途。两场景分别验收，"
        "每场景两轮各120秒、共24,000个请求；时延从预定到达到完整HTTP响应，包含调度滞后、代理、排队、编码和推理。"
        "风控测量包含跨区域SSH转发，SOP压测客户端与模型同机；均未覆盖生产外部入口和特征查询。"
    )
    quality_note = (
        "风控输入为原始200特征，SOP每次保留完整200候选。Karpo 4B、vLLM 0.34.0、FP8按通道量化，"
        "每卡独立完整模型，最大活跃序列数4，保留双向全注意力与FP32输出头。"
        "SOP的104条质量选择与原RTX结果全部一致；所有正确率均来自合成样例，不能代替真实业务数据验收。"
    )
    resource_note = (
        f"截至{state['utc']}，本项目全部{state['instance_count']}台EC2已stopped。"
        "本次停止5台运行实例，另1台原本已停止；实例、模型所在EBS及测试备份保留。"
        "实例计算计费已停止，保留的EBS和快照等仍可能计费。"
    )
    sop_note = (
        "SOP在1／2／4／8卡与最大batch 1／4之间完成对照。四卡H200在100 RPS到达下的稳态有效吞吐为68 RPS，"
        "只有八卡通过本轮100 RPS验收；5／6／7卡未测。batch 1与4的八卡探测P50接近，按冻结规则选中4。"
        "利用率每2秒采样，正式阶段取后75%的到达窗口；nvidia-smi利用率不是峰值FLOPS利用率。"
    )
    cost_note = (
        "费用按720小时/月估算，仅列各方案运行时的EC2计算费用，不含额外保留的测试实例、EBS、网络和IPv4。"
        "Spot价格会变化。八卡H200整机计费，即使只启用部分GPU也不会降低该实例费用；当前实例均已停止。"
    )
    links = [
        ("四RTX风控结果", "experiments/vllm-rps100-20261120/RTX4.zh.md"),
        ("风控优化依据", "experiments/vllm-rps100-20261120/OPTIMIZATION.zh.md"),
        ("H200 SOP详细HTML", "experiments/vllm-sop-h200-20261120/report.html"),
        ("H200 SOP文字报告", "experiments/vllm-sop-h200-20261120/RESULTS.zh.md"),
        ("SOP复现说明", "experiments/vllm-sop-h200-20261120/REPRODUCE.zh.md"),
        ("EC2停止记录", "operations/stop-20261120/infrastructure-stopped.json"),
        ("原系列模型报告与完整决策示例", "experiments/family-20261120/report.html"),
    ]
    markdown = "\n\n".join([
        "# Karpo风控与SOP：当前验收结果",
        "**风控四RTX、SOP八H200均通过各自的100 RPS验收；本项目EC2已全部停止。**",
        resource_note, "## 两场景正式结果", scope, md_table(headers, rows),
        "## 质量与输入", quality_note, md_table(q_headers, quality_rows),
        "## SOP卡数与GPU利用率", sop_note, md_table(g_headers, gpu_rows),
        "## 运行时参考费用", cost_note, md_table(cost_headers, cost_rows),
        "风控价格复核：" + prices[0][1]["utc"] + "；H200价格复核：" + prices[1][1]["utc"] + "。",
        "## 已停止的EC2", md_table(i_headers, i_rows),
        "## 报告与证据", " · ".join(f"[{label}]({url})" for label, url in links),
        "历史实验目录中的实例状态和方案结论是当时的记录。当前资源状态以上述停机记录为准；"
        "原始测试请求及冻结源码保持不变。修改前的文档另存于`operations/stop-20261120/previous/`。",
        "重新生成当前总报告：`python scripts/report.py`。",
    ]) + "\n"
    (ROOT / "RESULTS.zh.md").write_text(markdown)
    data = {
        "resource_utc": state["utc"], "all_project_ec2_stopped": True, "instances": instances,
        "performance": compact, "quality": quality_rows, "gpu": gpu_rows,
        "prices": [{"scheme": name, **{k: v for k, v in p.items() if k in (
            "utc", "spot_usd_per_hour", "on_demand_usd_per_hour", "month_hours"
        )}} for name, p in prices],
        "sources": {"risk": str((RTX / "data.json").relative_to(ROOT)),
                    "sop": str((H200 / "data.json").relative_to(ROOT)),
                    "resources": str((OPS / "infrastructure-stopped.json").relative_to(ROOT))},
    }
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    cards = "".join(
        f'<article><span>{label}</span><strong>{value}</strong><p>{note}</p></article>'
        for label, value, note in [
            ("风控 · 四RTX", "100 RPS",
             "两轮P50 " + " / ".join(f"{r['p50_ms']:.2f}" for r in risk_rounds) + "ms · 零失败"),
            ("SOP · 八H200", "100 RPS",
             "两轮P50 " + " / ".join(f"{r['p50_ms']:.2f}" for r in sop_rounds) + "ms · 零失败"),
            ("资源状态", f"{len(instances)} / {len(instances)} 已停止", "实例和EBS保留"),
        ]
    )
    content = (
        '<header><p>Karpo Decision Benchmark · 2026-11-20</p><h1>风控与SOP分别通过100 RPS验收</h1>'
        '<p>原始输入 · Karpo 4B · vLLM FP8 · 端到端P50≤500ms</p>'
        '<nav><a href="#performance">性能</a><a href="#quality">质量</a><a href="#gpu">GPU</a>'
        '<a href="#cost">费用</a><a href="#resources">停机记录</a>'
        '<a href="data.json" download>下载数据</a><button onclick="window.print()">打印 / PDF</button></nav></header>'
        f'<div class="cards">{cards}</div><p class="status">{html.escape(resource_note)}</p>'
        '<section id="performance"><h2>两场景正式结果</h2><p>' + html.escape(scope) + "</p>"
        + table(headers, rows) + '</section><section id="quality"><h2>质量与原始输入</h2><p>'
        + html.escape(quality_note) + "</p>" + table(q_headers, quality_rows)
        + '</section><section id="gpu"><h2>SOP卡数与GPU利用率</h2><p>' + html.escape(sop_note)
        + "</p>" + table(g_headers, gpu_rows) + '</section><section id="cost"><h2>运行时参考费用</h2><p>'
        + html.escape(cost_note) + "</p>" + table(cost_headers, cost_rows)
        + '<p>价格原始记录：<a href="operations/stop-20261120/risk-pricing.json">四RTX</a> · '
        '<a href="experiments/vllm-sop-h200-20261120/results/pricing-final.json">八H200</a></p>'
        + '</section><section id="resources"><h2>本项目EC2已全部停止</h2>'
        + table(i_headers, i_rows) + '</section><section><h2>详细报告与历史记录</h2><div class="links">'
        + "".join(f'<a href="{url}">{label} →</a>' for label, url in links)
        + '</div><p>历史报告保留当时的状态与测量结果；当前资源状态以本页的停机记录为准。</p></section>'
        '<footer><a href="RESULTS.zh.md">文字报告</a> · <a href="README.md">项目说明</a>'
        f' · 停机核对：{html.escape(state["utc"])}</footer>'
    )
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Karpo风控与SOP验收结果 · EC2全部停止</title>
<style>
:root{color-scheme:light;--ink:#18343d;--muted:#526970;--line:#dce5e7}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f4f7f8;color:var(--ink);font:15px/1.75 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1250px;margin:auto;padding:38px 24px}h1{font-size:clamp(25px,4vw,38px);line-height:1.4}
h2{font-size:23px}p{color:var(--muted)}a{color:#096b64}nav{display:flex;flex-wrap:wrap;gap:20px;align-items:center}
button{font:inherit;color:#096b64;background:white;border:1px solid var(--line);border-radius:6px;padding:5px 13px;cursor:pointer}
a:focus-visible,button:focus-visible{outline:3px solid #13857b;outline-offset:4px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:30px 0}
article,section{background:white;border:1px solid var(--line);border-radius:12px;padding:24px}article strong{display:block;font-size:30px;margin:9px 0;color:#096b64}article p{font-size:13px;margin:0}
.status{padding:17px 22px;border-left:4px solid #13857b;background:#e9f4ef}section{margin:24px 0;scroll-margin-top:20px}
.table{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);white-space:nowrap}th{background:#f0f5f6}
.links{display:grid;grid-template-columns:1fr 1fr;gap:12px}.links a{padding:10px;background:#f4f7f8;border-radius:6px}footer{font-size:12px;color:var(--muted);margin-top:30px}
@media(max-width:760px){main{padding:22px 14px}.cards,.links{grid-template-columns:1fr}section{padding:18px}nav{gap:14px}}
@media print{body{background:white}main{padding:0}nav{display:none}section,article{break-inside:avoid}th,td{white-space:normal;padding:7px;font-size:11px}.table{overflow:visible}}
</style></head><body><main>""" + content + "</main></body></html>"
    (ROOT / "report.html").write_text(page)
    print(json.dumps({"scenarios_passed": 2, "formal_requests": sum(r["attempts"] for r in compact),
                      "all_ec2_stopped": True, "instances": len(instances)}))


if __name__ == "__main__":
    main()
