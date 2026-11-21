"""Current overview, with expanded risk validation separated from historical tests."""
import html
import json
from pathlib import Path

from project_report import md_table, table

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-risk-rtx4-20261121"


def load(path):
    return json.loads(path.read_text())


def main():
    data = load(EXP / "data.json")
    proof = load(EXP / "results/verification.json")
    assert proof["verified_requests"] == data["verified_requests"]
    assert proof["accepted"] == data["accepted"]
    q = data["quality"]
    formal = [s for s in data["stages"] if s["name"] in ("confirm", "repeat")]
    state = data["infrastructure"]
    price = load(EXP / "operations/pricing.json")
    running = [i for i in state["instances"] if i["state"] == "running"]
    verdict = "通过" if data["accepted"] else "未通过"
    conclusion = (f"新版薅羊毛风控{verdict}验收：质量正确率{q['accuracy_all_attempts']:.2%}，"
                  f"100 RPS性能{'通过' if data['performance_passed'] else '未通过'}。")
    topology = ("原Karpo 4B、vLLM 0.34.0、FP8，四张RTX PRO 6000 Blackwell 96GB全部位于"
                "俄亥俄us-east-2c。一台g7e.12xlarge提供两卡，两台g7e.2xlarge各提供一卡；"
                "每卡独立模型副本，最大活跃序列数4。H100与H200均未参与本轮。")
    quality = (f"完整200特征、45类合成行为。quality集540个案例各测两种候选顺序，共1,080请求，"
               f"正确{q['correct']}，失败{q['failures']}。正常行为被干预"
               f"{q['benign_interventions']}/{q['benign_attempts']}；作弊行为被放行"
               f"{q['abuse_allowed']}/{q['abuse_attempts']}。"
               "tune和quality隔离实体与种子，但共享场景模板；本结果不能代表生产准确率。")
    scope = ("100 RPS指每秒固定到达100个请求。两轮各120秒，完整保留输入和候选；"
             "端到端从预定到达到完整HTTP响应，包含调度滞后、代理、排队、编码、推理和实例间私网。"
             "客户端位于双卡主机，生产入口与在线特征查询未纳入；失败全部计入验收。")
    resource = (f"资源核对时间：{state['utc']}。本项目{len(state['instances'])}台EC2中"
                f"{len(running)}台运行，其余{len(state['instances'])-len(running)}台停止。"
                "本轮四RTX实例保留，运行实例继续产生费用。")
    cost = (f"本轮三台RTX实例Spot参考价合计{price['spot_usd_per_hour']:.4f}美元/小时"
            f"（720小时约{price['spot_usd_per_hour']*720:,.2f}美元）；"
            f"按需{price['on_demand_usd_per_hour']:.4f}美元/小时"
            f"（约{price['on_demand_usd_per_hour']*720:,.2f}美元/月）。"
            f"报价时间{price['utc']}，不含EBS、快照、网络、IPv4及其他保留资源；Spot价格会变化。")
    headers = ["正式轮次", "到达RPS", "稳态有效RPS", "P50 ms", "P95 ms", "正确/全部", "失败"]
    rows = [[s["name"], 100, f"{s['steady_valid_rps']:.2f}", f"{s['p50_ms']:.2f}",
             f"{s['p95_ms']:.2f}", f"{s['correct']}/{s['attempts']}", s["failures"]] for s in formal]
    ih = ["实例", "型号", "可用区", "状态", "本轮GPU"]
    ir = [[i["id"], i["type"], i["az"], i["state"], i["test_gpu_count"]] for i in state["instances"]]
    history = ("旧版风控四RTX曾通过验收：合成质量97.08%，两轮100 RPS的P50为65.86/64.92ms。"
               "旧输入及策略与新版不同，该结论不能沿用为新版薅羊毛识别效果。"
               "SOP最近一次为八H200，质量94.23%，两轮100 RPS的P50为76.58/76.24ms；"
               "本轮未重测SOP。两项历史结果及当时停机记录保留在原实验目录。")
    links = [
        ("新版风控详细HTML", "experiments/promo-risk-rtx4-20261121/report.html"),
        ("新版风控文字报告", "experiments/promo-risk-rtx4-20261121/RESULTS.zh.md"),
        ("完整输入与模型实际输出", "experiments/promo-risk-rtx4-20261121/results/decision-example.json"),
        ("逐条核验记录", "experiments/promo-risk-rtx4-20261121/results/verification.json"),
        ("200特征与行为场景", "experiments/promo-risk-20261121/report.html"),
        ("旧版四RTX风控结果", "experiments/vllm-rps100-20261120/RTX4.zh.md"),
        ("SOP八H200结果", "experiments/vllm-sop-h200-20261120/report.html"),
        ("当前资源快照", "experiments/promo-risk-rtx4-20261121/operations/final-infrastructure.json"),
        ("本轮价格快照", "experiments/promo-risk-rtx4-20261121/operations/pricing.json"),
    ]
    sections = [
        ("本轮配置与质量", topology + "\n\n" + quality),
        ("100 RPS实测", scope + "\n\n" + md_table(headers, rows)),
        ("当前资源与费用", resource + "\n\n" + cost + "\n\n" + md_table(ih, ir)),
        ("历史结果边界", history),
        ("报告与证据", " · ".join(f"[{label}]({url})" for label, url in links)),
    ]
    (ROOT / "RESULTS.zh.md").write_text(
        "# Karpo风控与SOP：当前验收结果\n\n**" + conclusion + "**\n\n"
        + "\n\n".join("## " + title + "\n\n" + body for title, body in sections)
        + "\n\n重新生成：`python scripts/report.py`。\n")
    (ROOT / "data.json").write_text(json.dumps({
        "current_risk": data, "prices": price,
        "sources": {"current_risk": str((EXP / "data.json").relative_to(ROOT)),
                    "historical_risk": "experiments/vllm-rps100-20261120/data.json",
                    "sop": "experiments/vllm-sop-h200-20261120/data.json"},
        "resource_utc": state["utc"], "all_project_ec2_stopped": not running,
        "instances": state["instances"],
    }, ensure_ascii=False, indent=2) + "\n")
    esc = html.escape
    links_html = " · ".join(f'<a href="{url}">{label}</a>' for label, url in links)
    (ROOT / "report.html").write_text(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Karpo新版风控实际验证</title>
<style>body{{font:16px/1.8 system-ui,sans-serif;max-width:1200px;margin:auto;padding:28px;background:#f4f7f8;color:#18343d}}
section{{background:white;border:1px solid #dce5e7;border-radius:12px;padding:24px;margin:24px 0}}
h1{{font-size:30px;line-height:1.5}}a{{color:#096b64}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:10px;text-align:left;border-bottom:1px solid #dce5e7;white-space:nowrap}}
@media(max-width:760px){{body{{padding:14px}}section{{padding:16px}}}}
@media print{{body{{background:white}}section{{break-inside:avoid}}}}</style></head><body>
<p>Karpo Decision Benchmark · 2026-11-21</p><h1>{esc(conclusion)}</h1>
<p><a href="RESULTS.zh.md">文字报告</a> · <a href="data.json" download>下载数据</a></p>
<section><h2>本轮配置与质量</h2><p>{esc(topology)}</p><p>{esc(quality)}</p></section>
<section><h2>100 RPS实测</h2><p>{esc(scope)}</p>{table(headers, rows)}</section>
<section><h2>当前资源与费用</h2><p>{esc(resource)}</p><p>{esc(cost)}</p>{table(ih, ir)}</section>
<section><h2>历史结果边界</h2><p>{esc(history)}</p></section>
<section><h2>报告与证据</h2><p>{links_html}</p></section></body></html>""")
    print(conclusion)


if __name__ == "__main__":
    main()
