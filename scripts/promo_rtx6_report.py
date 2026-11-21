"""Render six-GPU acceptance from independently verified real HTTP evidence."""
import html
import json
from pathlib import Path
import posixpath
import re
from urllib.parse import urlsplit

from project_report import md_table, table
from report_examples import EXAMPLE_CSS, example_html, example_markdown, examples

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"
ARCHITECTURE = "docs/assets/promo-rtx6-aws-architecture"


def architecture_markdown(prefix=""):
    return (
        f"[AWS部署说明]({prefix}docs/ARCHITECTURE.zh.md) · "
        f"[SVG原图]({prefix}{ARCHITECTURE}.svg) · "
        f"[高清PNG]({prefix}{ARCHITECTURE}.png)\n\n"
        f"![六RTX AWS实测部署架构：五台EC2，俄亥俄四卡、北弗吉尼亚两卡]"
        f"({prefix}{ARCHITECTURE}.svg)\n\n"
        "代理与压测客户端位于双卡EC2 A，每张GPU运行一个完整模型副本。"
        "图示为测试期间部署；入口代理随压测结束退出，EC2保留状态以资源快照为准。"
    )


def read(path):
    return json.loads(path.read_text())


def stage(run, name):
    return next(s for s in run["stages"] if s["name"] == name)


def main():
    selected_examples = examples()
    proof = read(EXP / "results/verification.json")
    final, old = proof["runs"][0], proof["four_gpu_comparison"]
    infra = read(EXP / "operations/final-infrastructure.json")
    pricing = read(EXP / "operations/pricing.json")
    quality = final["quality"]
    verdict = "通过" if final["accepted"] else "未通过"
    title = f"六RTX实际模型验证：{verdict}验收"
    conclusions = [
        f"六卡固定batch=4、CUDA Graph powers。两轮100 RPS端到端P50为"
        f"{stage(final,'confirm')['p50_ms']:.2f}／{stage(final,'repeat')['p50_ms']:.2f}ms，"
        f"有效吞吐为{stage(final,'confirm')['steady_valid_rps']:.2f}／"
        f"{stage(final,'repeat')['steady_valid_rps']:.2f} RPS，"
        f"失败{stage(final,'confirm')['failures']}／{stage(final,'repeat')['failures']}次，整体{verdict}验收。",
        f"合成质量集最终决策正确率{quality['accuracy_all_attempts']:.2%}（{quality['correct']}/{quality['attempts']}），"
        f"同批原始Karpo选择为{final['raw_quality']['accuracy_all_attempts']:.2%}。"
        f"应复核识别{quality['groups']['action']['review']['correct']}/264，正常行为误干预"
        f"{quality['benign_interventions']}/{quality['benign_attempts']}，作弊行为放行"
        f"{quality['abuse_allowed']}/{quality['abuse_attempts']}。",
        "每卡一个完整Karpo 4B FP8副本，保留原FP32决策头、完整双向注意力、原提示及全部200特征。"
        "复核分数阈值保持0.1628260940455841，原始answers与校准后decision分开记录；"
        "最终决策效果包含分数后处理，不是原始模型argmax准确率。",
    ]
    network = (
        "实际部署为us-east-2c四张RTX加us-east-1d两张RTX PRO 6000 Blackwell Server Edition 96GB，"
        "共五台Spot EC2。俄亥俄Spot配额已满、四种RTX实例规格在三个可用区的按需容量均不足，"
        "因此新增两卡使用北弗吉尼亚Spot。两张远端卡经仅绑定loopback的SSH隧道访问。"
        "端到端从预定到达至完整HTTP响应，包含排队、编码、代理及跨区域SSH传输，"
        "不含生产外部入口与在线特征查询。四卡对照位于同可用区，六卡结果包含新增网络路径的影响。")
    acceptance = (
        "持续每秒固定到达100请求，不是100个请求同时在途。正式验收为两轮各120秒："
        "稳定有效吞吐≥99 RPS、端到端P50≤500ms、零失败，并检查尾段P50、发送滞后及积压趋势。"
        "质量和正式压力正确率均按全部尝试计算且须≥90%，失败不算正确。"
        "压力阶段循环360条tune请求，质量阶段单独统计1080条quality请求。"
        "延迟分位数仅统计有效响应，任何失败仍单独计入验收。")
    fh = ["配置", "轮次", "有效RPS", "P50 ms", "P95 ms", "失败/全部", "正确/全部"]
    fr = []
    for label, run in (("四RTX", old), ("六RTX", final)):
        for name in ("confirm", "repeat"):
            s = stage(run, name)
            fr.append([label, name, f"{s['steady_valid_rps']:.2f}", f"{s['p50_ms']:.2f}",
                       f"{s['p95_ms']:.2f}", f"{s['failures']}/{s['attempts']}",
                       f"{s['correct']/s['attempts']:.2%}"])
    qh = ["输出口径", "正确率", "review正确/全部", "正常误干预", "作弊放行", "换序不一致"]
    qr = []
    for label, q in (("四卡校准决策", old["quality"]), ("六卡原始模型选择", final["raw_quality"]),
                     ("六卡校准决策", final["quality"])):
        review = q["groups"]["action"]["review"]
        qr.append([label, f"{q['accuracy_all_attempts']:.2%}", f"{review['correct']}/{review['attempts']}",
                   f"{q['benign_interventions']}/{q['benign_attempts']}",
                   f"{q['abuse_allowed']}/{q['abuse_attempts']}", f"{q['order_disagreements']}/{q['valid_order_pairs']}"])
    quality_scope = (
        "逐字节复用上一轮1440条输入和真实编码参考：360条tune、1080条quality。"
        "quality是540个案例的两种候选顺序，本轮属于硬件扩容复测，不是新独立留出集。"
        "未重新选择提示或调整阈值。tune与quality共享45种合成行为模板，不能代表生产准确率。")
    gh = ["轮次", "副本", "区域", "平均GPU利用率", "采样数"]
    gr = [[r["stage"], r["replica"], r["region"], f"{r['mean_percent']:.2f}%", r["samples"]]
          for r in proof["gpu_utilization"]]
    ih = ["实例", "规格", "可用区", "本轮使用GPU数", "状态"]
    ir = [[r["id"], r["type"], r["az"], r["test_gpu_count"], r["state"]] for r in infra["instances"]]
    resources = f"核对时间{infra['utc']}：五台参与EC2保持运行、共六张RTX，其余五台项目EC2保持停止。"
    cost = (
        f"五台参与实例当前Spot计算参考价合计{pricing['spot_usd_per_hour']:.4f}美元/小时，"
        f"按720小时为{pricing['spot_usd_per_hour']*720:,.2f}美元/月。"
        f"同配置按需标价合计{pricing['on_demand_usd_per_hour']:.4f}美元/小时，"
        f"按720小时为{pricing['on_demand_usd_per_hour']*720:,.2f}美元/月。"
        "报价不含EBS、公网IPv4、跨区域数据传输及其他保留资源；Spot价格会变化。按需价格不代表当前有容量。")
    low = stage(final, "low-load")
    limits = (
        f"六卡40 RPS低负载P50为{low['p50_ms']:.2f}ms、失败{low['failures']}。"
        "本轮只验证六卡固定batch=4，未测试五卡、未重做batch搜索，不能据此认定六卡是绝对最低配置。"
        "复核阈值不会消除原模型的全部错误，完整混淆矩阵及错误请求保存在核验JSON。")
    sections = [
        ("部署与延迟范围", network, None, None),
        ("两轮正式100 RPS验证", acceptance, fh, fr),
        ("同一合成质量集复测", quality_scope, qh, qr),
        ("GPU利用率", "每轮后90秒，每两秒采样一次nvidia-smi GPU利用率。", gh, gr),
        ("适用范围", limits, None, None),
        ("EC2与费用", resources + cost, ih, ir),
    ]
    prefix = str(EXP.relative_to(ROOT)) + "/"
    links = "[完整输入输出](results/decision-example.json) · [逐条核验](results/verification.json) · " \
            "[复现说明](REPRODUCE.zh.md) · [资源与价格](operations/pricing.json) · [冻结计划](PLAN.zh.md)"
    markdown = "# " + title + "\n\n" + "\n\n".join(conclusions) + "\n\n"
    markdown += example_markdown(selected_examples["risk"], "../../") + "\n\n"
    for heading, body, headers, rows in sections:
        markdown += "## " + heading + "\n\n" + body + "\n\n"
        if heading == "部署与延迟范围":
            markdown += architecture_markdown("../../") + "\n\n"
        if headers:
            markdown += md_table(headers, rows) + "\n\n"
    markdown += f"共逐条核验{proof['verified_requests']:,}次请求和6份实际GPU引擎配置。\n\n{links}\n"
    (EXP / "RESULTS.zh.md").write_text(markdown)
    esc = html.escape
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>body{{font:16px/1.75 system-ui;max-width:1200px;margin:auto;padding:24px;background:#f3f6f8;color:#17313c}}
section{{background:white;padding:24px;margin:24px 0;border-radius:12px}}table{{width:100%;border-collapse:collapse}}
td,th{{padding:10px;text-align:left;border-bottom:1px solid #dce5e7;white-space:nowrap}}.table{{overflow:auto}}
a{{color:#096b64}}@media(max-width:700px){{body{{padding:12px}}section{{padding:12px}}}}</style></head><body><h1>{esc(title)}</h1>"""
    page = page.replace("</style>", """
figure.architecture{margin:20px 0 0}
.architecture img{display:block;width:100%;height:auto;border:1px solid #dce5e7;border-radius:8px}
.architecture figcaption{font-size:14px;color:#526174;margin-top:10px}
@media print{body{max-width:none;background:white;padding:0}figure.architecture{break-inside:avoid}}
</style>""")
    page = page.replace("</style>", EXAMPLE_CSS + "</style>")
    page += "".join(f"<p>{esc(p)}</p>" for p in conclusions)
    page += '<p><a href="RESULTS.zh.md">详细文档</a> · <a href="#risk-example">问题与回答示例</a> · <a href="data.json">汇总JSON</a> · <a href="results/decision-example.json">完整输入输出</a> · <a href="REPRODUCE.zh.md">复现说明</a></p>'
    page += example_html(selected_examples["risk"], "../../")
    for heading, body, headers, rows in sections:
        page += f"<section><h2>{esc(heading)}</h2><p>{esc(body)}</p>"
        if heading == "部署与延迟范围":
            page += (
                '<figure class="architecture">'
                f'<a href="../../{ARCHITECTURE}.svg" title="打开完整SVG架构图">'
                f'<img src="../../{ARCHITECTURE}.svg" width="1680" height="1340" '
                'alt="AWS六RTX实测部署：俄亥俄双卡EC2 A运行压测与代理，'
                '同区两台单卡EC2，跨区域SSH连接北弗吉尼亚两台单卡EC2"></a>'
                '<figcaption>代理与压测客户端位于双卡EC2 A；每张GPU运行一个完整模型副本。'
                '图示为测试期间部署，入口代理随压测结束退出。'
                '<br><a href="../../docs/ARCHITECTURE.zh.md">部署说明</a> · '
                f'<a href="../../{ARCHITECTURE}.svg">查看SVG原图</a> · '
                f'<a href="../../{ARCHITECTURE}.png" download>下载高清PNG</a>'
                '</figcaption></figure>'
            )
        if headers:
            page += table(headers, rows)
        page += "</section>"
    page += "</body></html>"
    (EXP / "report.html").write_text(page)
    data = {"verification": proof, "infrastructure": infra, "pricing": pricing,
            "accepted": final["accepted"], "conclusion": conclusions}
    for path in (EXP / "data.json", ROOT / "data.json"):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
    (ROOT / "RESULTS.zh.md").write_text("# " + title + "\n\n" + "\n\n".join(conclusions) +
        "\n\n" + md_table(fh, fr) + "\n\n" + network +
        "\n\n" + architecture_markdown() + "\n\n" + resources +
        f"\n\n[完整六卡报告]({prefix}RESULTS.zh.md) · [HTML]({prefix}report.html)\n\n"
        + example_markdown(selected_examples["risk"]) + "\n\n"
        + example_markdown(selected_examples["sop"]) + "\n\n"
        "SOP本轮未重测；八H200达标结果及示例见[报告](experiments/vllm-sop-h200-20261120/report.html)。\n")
    def root_link(match):
        attribute, target = match.groups()
        if not urlsplit(target).scheme and not target.startswith(("/", "#")):
            target = posixpath.normpath(prefix + target)
        return f'{attribute}="{target}"'

    root_page = re.sub(r'(href|src)="([^"]+)"', root_link, page).replace("</body>",
        example_html(selected_examples["sop"]) +
        '<p>SOP本轮未重测；<a href="experiments/vllm-sop-h200-20261120/report.html">历史八H200报告</a></p></body>')
    root_page = root_page.replace(
        '<a href="#risk-example">问题与回答示例</a>',
        '<a href="#risk-example">风控问题与回答</a> · <a href="#sop-example">SOP问题与回答</a>')
    (ROOT / "report.html").write_text(root_page)
    print(conclusions[0])


if __name__ == "__main__":
    main()
