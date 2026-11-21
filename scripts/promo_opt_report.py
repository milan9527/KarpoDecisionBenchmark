"""Render the three-stage experiment from independently verified raw results."""
import html
import json
from pathlib import Path

from project_report import md_table, table

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-opt-20261121"


def read(path):
    return json.loads(path.read_text())


def stage(run, name):
    return next(row for row in run["stages"] if row["name"] == name)


def formal_rows(run):
    return [[run["name"], s, f"{stage(run,s)['steady_valid_rps']:.2f}",
             f"{stage(run,s)['p50_ms']:.2f}", f"{stage(run,s)['p95_ms']:.2f}",
             stage(run,s)["failures"], f"{stage(run,s)['correct']/stage(run,s)['attempts']:.2%}"]
            for s in ("confirm", "repeat")]


def main():
    proof = read(EXP / "results/verification.json")
    runs = {row["name"]: row for row in proof["runs"]}
    old, raw, final = [runs[name] for name in (
        "old-control", "review-confirmed", "review-calibrated-confirmed")]
    baseline = runs["review-fresh-baseline"]
    performance = read(EXP / "results/performance-selection.json")
    review = read(EXP / "results/review-selection.json")
    calibration = read(EXP / "review-calibration-v1.json")
    infra = read(EXP / "operations/final-infrastructure.json")
    final_q = stage(final, "quality")
    verdict = "通过" if final["accepted"] else "未通过"
    title = f"四RTX优化验证：最终{verdict}验收"
    paragraphs = [
        f"最终配置固定batch {final['batch']}、CUDA Graph {final['graph']}、原提示，增加复核分数阈值。"
        f"实际HTTP服务的新留出集最终决策正确率{final_q['accuracy']:.2%}（{final_q['correct']}/{final_q['attempts']}）；"
        f"同批请求的原始Karpo选择正确率为{final['raw_quality']['accuracy_all_attempts']:.2%}。"
        f"两轮100 RPS性能{'通过' if final['performance_passed'] else '未通过'}，"
        f"计入失败的压力正确率{'通过' if final['pressure_quality_passed'] else '未通过'}，整体{verdict}验收。",
        "硬件固定为俄亥俄us-east-2c四张RTX PRO 6000 Blackwell 96GB，"
        "一台g7e.12xlarge双卡、两台g7e.2xlarge单卡，每卡独立Karpo 4B FP8副本，"
        "vLLM 0.34.0、原始FP32决策头、完整双向输入、无答案缓存。H100/H200未参与。",
        "100 RPS为持续固定到达100请求/秒。端到端计时从计划到达至完整HTTP返回，"
        "包括排队、编码、代理及实例间私网；不含生产入口和在线特征查询。"
        "P50≤500ms、稳定有效RPS≥99、零失败，并检查尾段延迟、发送滞后和积压趋势。"
        "质量与正式压力正确率均须≥90%；失败计入分母。",
    ]
    fh = ["测试", "轮次", "有效RPS", "P50 ms", "P95 ms", "失败", "正确/全部"]
    fr = formal_rows(old) + formal_rows(raw) + formal_rows(final)
    lh = ["40 RPS对照", "有效RPS", "P50 ms", "失败", "正确/全部"]
    lr = [[r["name"], f"{stage(r,'low-load')['steady_valid_rps']:.2f}",
           f"{stage(r,'low-load')['p50_ms']:.2f}", stage(r,"low-load")["failures"],
           f"{stage(r,'low-load')['correct']/stage(r,'low-load')['attempts']:.2%}"] for r in (raw, final)]
    sh = ["batch", "Graph", "有效RPS", "P50 ms", "失败", "探测通过"]
    sr = [[c["batch"], c["graph"], f"{c['probe']['steady_valid_rps']:.2f}",
           f"{c['probe']['p50_ms']:.2f}", c["probe"]["failures"],
           "是" if c["probe"]["passes_probe"] else "否"] for c in performance["candidates"]]
    th = ["提示版本", "调参正确率", "review正确/全部", "平均JSON字节"]
    tr = [[c["variant"], f"{c['tune']['accuracy']:.2%}",
           f"{c['review_correct']}/{c['review_attempts']}", f"{c['mean_json_bytes']:.0f}"] for c in review["candidates"]]
    qh = ["新留出集", "正确率", "review正确/全部", "正常行为被干预", "作弊行为被放行", "换序不一致"]
    qr = []
    for label, q in (("原提示/共同Graph", baseline["quality"]),
                     ("原提示/正式配置", raw["quality"]),
                     ("校准服务中的原始模型选择", final["raw_quality"]),
                     ("校准服务最终决策", final["quality"])):
        r = q["groups"]["action"]["review"]
        qr.append([label, f"{q['accuracy_all_attempts']:.2%}", f"{r['correct']}/{r['attempts']}",
                   f"{q['benign_interventions']}/{q['benign_attempts']}",
                   f"{q['abuse_allowed']}/{q['abuse_attempts']}",
                   f"{q['order_disagreements']}/{q['valid_order_pairs']}"])
    gh = ["测试", "轮次", "副本", "平均GPU利用率"]
    gr = [[r["run"], r["stage"], r["replica"], f"{r['mean_percent']:.2f}%"]
          for r in proof["gpu_utilization"]]
    old_tokens = [r["length"] for r in read(EXP / "results/encodings/old.json")]
    final_tokens = [r["length"] for r in read(EXP / "results/encodings/review-baseline.json")]
    control = (f"旧360条输入逐字节复用，真实编码与历史逐条相同。旧质量仍为"
               f"{stage(old,'quality')['accuracy']:.2%}；当前正式性能"
               f"{'通过' if old['performance_passed'] else '未通过'}。"
               f"旧输入为{min(old_tokens)}～{max(old_tokens)} tokens，"
               f"最终新版输入为{min(final_tokens)}～{max(final_tokens)} tokens。"
               "新版原输入完整保留200个特征，batch/Graph扫描仅改服务配置。"
               "用户随后要求停止调整batch，后续固定原batch 4/powers配置继续复核优化。")
    tuning = (f"已完成{len(performance['candidates'])}组性能扫描，均使用tune集。"
              "剩余batch搜索按用户要求停止，已完成结果保留；固定batch 4继续后续验证。"
              "powers在不超过batch上限时覆盖1/2/4/8/16批大小，dense补全1至batch的中间档位，"
              "实际捕获token档位以engine-config.json为准。未把探测结果当作正式验收。")
    review_text = ("提示候选分别为原描述、分条条件、自然语言条件、allow排除条件、平铺字段。"
                   "全部保留200个特征值、六类动作、业务阈值及优先级；flat仅改变字段布局。"
                   "用360条原tune请求选择版本，选择记录冻结后才推理新留出集。"
                   "新留出集含540个案例、两种候选顺序，共1080请求；实体、种子和请求与旧集隔离，"
                   "但共享45种合成场景模板，结果不能代表生产准确率。"
                   "先在共同Graph配置下比较原提示与选定提示，再对最终提示单独捕获Graph并正式压测。")
    calibration_text = (
        "提示修改均未优于原提示，因此最终保留原输入。只在原模型选择allow时，若"
        f"p(review)/(p(review)+p(allow)) ≥ {calibration['selected']['threshold']:.16f}，"
        "将最终决策升级为review；其他动作不变。原始answers及概率完整保留，decision单独返回。"
        f"阈值仅用360条tune拟合，调参正确率{calibration['selected']['accuracy']:.2%}，"
        f"在{calibration['frozen_utc']}冻结，早于新留出集第一次模型推理。"
        "这是基于模型分数的决策后处理，不是原模型argmax准确率，也不是模型微调。"
        "不增加GPU调用；代理后处理计时包含读取及解析响应、计算阈值，"
        "不含最终JSON序列化。正式阶段该计时P50为"
        f"{stage(final,'confirm')['calibration_p50_ms']:.4f}／"
        f"{stage(final,'repeat')['calibration_p50_ms']:.4f}ms，完整服务开销仍以端到端测试为准。")
    remaining = "；".join(f"应{r['expected']}却选{r['actual']}：{r['count']}条"
                         for r in final["quality"]["confusion"] if r["expected"] != r["actual"])
    calibration_text += "新留出集剩余错误为：" + (remaining or "无") + "。"
    running = [r for r in infra["instances"] if r["state"] == "running"]
    resources = f"核对时间{infra['utc']}，本项目{len(running)}台EC2保持运行；按用户要求未关机。"
    ih = ["实例", "类型", "可用区", "状态"]
    ir = [[r["id"], r["type"], r["az"], r["state"]] for r in infra["instances"]]
    sections = [
        ("旧输入环境对照", control, None, None),
        ("完整新版输入：已完成的batch与CUDA Graph对照", tuning, sh, sr),
        ("复核判断：调参集选择", review_text, th, tr),
        ("复核阈值校准", calibration_text, None, None),
        ("独立新留出集", "review为应复核类的召回计数；失败不算正确，也不当作安全拦截。", qh, qr),
        ("40 RPS低负载对照", "同完整输入、同batch 4；此处正确率来自循环tune请求，不是独立留出集。", lh, lr),
        ("两轮120秒正式验证", "延迟分位数取有效响应，所有失败另计并导致性能验收失败。", fh, fr),
        ("正式阶段GPU利用率", "取每轮后90秒，每两秒采样。", gh, gr),
        ("EC2状态", resources, ih, ir),
    ]
    links = "[逐条核验](results/verification.json) · [完整输入输出示例](results/decision-example.json) · [性能选择](results/performance-selection.json) · " \
            "[提示选择](results/review-selection.json) · [阈值冻结](review-calibration-v1.json) · " \
            "[原计划](PLAN.zh.md) · [停止batch搜索的修订](AMENDMENT-STOP-BATCH.zh.md) · [校准验证说明](CALIBRATION.zh.md)"
    text = "# " + title + "\n\n" + "\n\n".join(paragraphs) + "\n\n"
    for heading, body, headers, rows in sections:
        text += "## " + heading + "\n\n" + body + "\n\n"
        if headers:
            text += md_table(headers, rows) + "\n\n"
    text += f"核验{proof['verified_requests']:,}次请求、{proof['engine_proofs']}份GPU引擎配置。\n\n{links}\n"
    (EXP / "RESULTS.zh.md").write_text(text)
    esc = html.escape
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>body{{font:16px/1.75 system-ui;max-width:1200px;margin:auto;padding:24px;background:#f3f6f8;color:#17313c}}
section{{background:white;padding:24px;margin:24px 0;border-radius:12px}}table{{width:100%;border-collapse:collapse}}
td,th{{padding:10px;text-align:left;border-bottom:1px solid #dce5e7;white-space:nowrap}}.table{{overflow:auto}}
a{{color:#096b64}}@media(max-width:700px){{body{{padding:12px}}section{{padding:12px}}}}</style></head><body>
<h1>{esc(title)}</h1>"""
    page += "".join(f"<p>{esc(p)}</p>" for p in paragraphs)
    page += '<p><a href="RESULTS.zh.md">详细文档</a> · <a href="data.json">下载完整结果</a> · <a href="results/decision-example.json">完整输入输出</a> · <a href="results/verification.json">逐条核验</a></p>'
    for heading, body, headers, rows in sections:
        page += f"<section><h2>{esc(heading)}</h2><p>{esc(body)}</p>"
        if headers:
            page += table(headers, rows)
        page += "</section>"
    page += "</body></html>"
    (EXP / "report.html").write_text(page)
    data = {"verification": proof, "performance_selection": performance, "review_selection": review,
            "review_calibration": calibration,
            "accepted": final["accepted"], "infrastructure": infra, "conclusion": paragraphs[0]}
    (EXP / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    # Root overview points to this complete experiment; historical reports stay frozen.
    prefix = str(EXP.relative_to(ROOT)) + "/"
    (ROOT / "RESULTS.zh.md").write_text("# " + title + "\n\n" + "\n\n".join(paragraphs) +
        "\n\n" + md_table(fh, fr) + "\n\n" + resources +
        f"\n\n[完整优化报告]({prefix}RESULTS.zh.md) · [HTML]({prefix}report.html)\n\n"
        "SOP本轮未重测；历史八H200结果保留在[原报告](experiments/vllm-sop-h200-20261120/report.html)。\n")
    root_page = page.replace('href="', f'href="{prefix}')
    root_page = root_page.replace("</body>", '<p>SOP本轮未重测；'
        '<a href="experiments/vllm-sop-h200-20261120/report.html">历史八H200报告</a></p></body>')
    (ROOT / "report.html").write_text(root_page)
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(paragraphs[0])


if __name__ == "__main__":
    main()
