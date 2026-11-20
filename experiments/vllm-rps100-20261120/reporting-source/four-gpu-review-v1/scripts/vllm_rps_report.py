"""Recompute completed stages and publish a clearly provisional or final report."""
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

from markdown_it import MarkdownIt

from benchmark import validate
from vllm_rps_benchmark import dump, summarize

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"


def load(path):
    return json.loads(path.read_text())


def main():
    fixtures = {i["id"]: i for scenario in ("risk", "sop") for i in load(EXP / f"{scenario}-requests.json")}
    encoding = {i["request_id"]: i for i in load(EXP / "encoding-reference.json")}
    reference = {}
    for scenario in ("risk", "sop"):
        raw = ROOT / f"experiments/vllm-single-20261120/results/measurements/native-initial/{scenario}/quality/requests.jsonl"
        reference.update({r["request_id"]: r for r in map(json.loads, raw.read_text().splitlines())})
    runs, performance, quality = [], [], []
    attempts = valid = pending_sync = 0
    for status_path in sorted((EXP / "results/runs").glob("*/status.json")):
        directory = status_path.parent
        state = load(status_path)
        manifest = load(directory / "manifest.json")
        for path, expected in manifest["files"].items():
            archived = EXP / "frozen" / manifest["revision"] / path
            source = archived if archived.exists() else ROOT / path
            assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, source
        runs.append({"name": directory.name, "status": state["status"],
                     "current": state.get("current"), "error": state.get("error"),
                     "revision": manifest["revision"]})
        for summary_path in sorted(directory.glob("b*/r*/*/*/summary.json")):
            stored = load(summary_path)
            raw = summary_path.with_name("requests.jsonl")
            rows = [json.loads(line) for line in raw.read_text().splitlines()]
            if len(rows) != stored["attempts"]:
                pending_sync += 1
                continue
            name = summary_path.parent.name
            scenario = summary_path.parent.parent.name
            replicas = int(summary_path.parents[2].name[1:])
            batch = int(summary_path.parents[3].name[1:])
            common = {"run": directory.name, "scenario": scenario, "replicas": replicas, "batch": batch}
            for row in rows:
                attempts += 1
                item = fixtures[row["request_id"]]
                assert item["request_sha256"] == row["request_sha256"]
                assert math.isclose(row["latency_ms"],
                                    (row["end_offset_s"] - row["scheduled_offset_s"]) * 1000, abs_tol=1e-5)
                if row["valid"]:
                    valid += 1
                    assert validate(row["response"], item) == row["correct"]
                    metrics = row["response"]["metrics"]
                    expected = encoding[row["request_id"]]
                    assert metrics["tokens_sha256"] == expected["tokens_sha256"]
                    assert metrics["marker"] == expected["marker"]
                    assert row["response"]["usage"]["input_tokens"] == expected["length"]
                    assert metrics["proxy_replica"] == metrics["replica_id"]
                    assert 0 <= metrics["proxy_replica"] < replicas
            if name == "quality":
                correct = sum(r["correct"] for r in rows)
                assert correct == stored["correct"]
                same = new_errors = 0
                differences = []
                for row in rows:
                    old = reference[row["request_id"]]
                    old_choices = {k: v["choice"] for k, v in old["response"]["answers"].items()}
                    actual = {k: v["choice"] for k, v in row.get("response", {}).get("answers", {}).items()}
                    same += old_choices == actual
                    new_errors += old["correct"] and not row["correct"]
                    if old_choices != actual:
                        differences.append({"request_id": row["request_id"], "reference": old_choices,
                                            "actual": actual, "correct": row["correct"]})
                parity = {"same_choices": same, "observations": len(rows),
                          "new_errors": new_errors, "differences": differences}
                dump(summary_path.with_name("parity-vs-native.json"), parity)
                quality.append({**common, **stored, "same_choices": same, "new_errors": new_errors})
            else:
                fresh = summarize(rows, stored["target_rps"], stored["offered_seconds"], stored["elapsed_with_drain_s"])
                assert fresh == stored, summary_path
                performance.append({**common, "stage": name, **stored})
    verification = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "verified_attempts": attempts, "valid": valid, "failures": attempts - valid,
                    "verified_performance_stages": len(performance), "verified_quality_stages": len(quality),
                    "awaiting_sync_stages": pending_sync}
    accepted = [r for r in performance if r["passes_performance_acceptance"]]
    confirmed = []
    for row in quality:
        key = (row["run"], row["scenario"], row["batch"], row["replicas"])
        matches = [p for p in accepted if
                   (p["run"], p["scenario"], p["batch"], p["replicas"]) == key]
        if row["failures"] == 0 and row["accuracy"] > .9 and len(matches) >= 2:
            confirmed.append({"run": row["run"], "scenario": row["scenario"],
                              "batch": row["batch"], "replicas": row["replicas"],
                              "quality_accuracy": row["accuracy"],
                              "accepted_stages": [p["stage"] for p in matches],
                              "p50_ms": [p["p50_ms"] for p in matches]})
    verification["confirmed_scenarios"] = sorted({c["scenario"] for c in confirmed})
    labels = {"risk": "风控", "sop": "SOP"}
    lines = [
        "# 固定100 RPS与端到端P50≤500ms优化", "",
        f"数据核对时间：{verification['utc']}。当前有{len(accepted)}个性能阶段满足正式时长和性能门槛；最终验收还需独立复测和质量检查。", "",
        "风险与SOP分别测试，完整200特征／200候选不变。每秒固定发送指定数量的请求，包含排队、编码、模型和本次HTTP链路。",
        "表中P50仅针对有效完整响应；任何拒绝、超时或无效响应均使该阶段不达标。20秒结果为探测，正式验收至少120秒。",
        "端到端时延保守包含客户端发起滞后；另保留实际发出至完整HTTP响应的时间。生产外部入口和特征查询尚未纳入本次部署。", "",
        "## 实验状态", "",
    ]
    if confirmed:
        lines[2] = (
            f"数据核对时间：{verification['utc']}。"
            f"以下{len(confirmed)}组通过合成质量检查及至少两次120秒性能测试："
        )
        lines[3:3] = [""] + [
            f"- {labels[c['scenario']]}：{c['run']}，{c['replicas']}个GPU副本，"
            f"100 RPS，正式测试P50为{'／'.join(f'{p:.1f}ms' for p in c['p50_ms'])}，"
            f"合成正确率{c['quality_accuracy']:.2%}。"
            for c in confirmed
        ] + [""]
    for run in runs:
        lines.append(f"- {run['name']}：{run['status']}；源码版本{run['revision']}。")
    lines += ["", "## 固定100 RPS结果", "",
              "| 实验 | 场景 | GPU副本 | 最大序列数 | 持续秒数 | 有效响应/请求 | 稳态完成RPS | 有效响应P50 | P95 | 性能验收 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in performance:
        if row["target_rps"] != 100:
            continue
        p50 = f"{row['p50_ms']:.1f}ms" if row["p50_ms"] is not None else "—"
        p95 = f"{row['p95_ms']:.1f}ms" if row["p95_ms"] is not None else "—"
        lines.append(f"| {row['run']} | {labels[row['scenario']]} | {row['replicas']} | {row['batch']} | "
                     f"{row['offered_seconds']:g} | {row['valid']}/{row['attempts']} | {row['steady_valid_rps']:.2f} | "
                     f"{p50} | {p95} | {'通过该阶段' if row['passes_performance_acceptance'] else '未通过'} |")
    lines += ["", "## 合成质量", "",
              "| 实验 | 场景 | GPU副本 | 最大序列数 | 正确/总数 | 同原生决策 | 新增错误 |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for row in quality:
        lines.append(f"| {row['run']} | {labels[row['scenario']]} | {row['replicas']} | {row['batch']} | "
                     f"{row['correct']}/{row['attempts']} | {row['same_choices']}/{row['attempts']} | {row['new_errors']} |")
    lines += ["", "合成样例不能代表生产准确率。", "", "## 资源与证据", "",
              "- 按用户要求保持EC2运行；本轮脚本不执行实例关机。",
              "- 各运行目录保存完整请求、质量差异、GPU监控、启动命令、软件环境及实际量化内核选择。",
              "- FP8 profiler显示矩阵计算占GPU kernel时间约67%（风控）／59%（SOP）；诊断时间不用于性能验收。",
              "- NVFP4的MoE预设未量化Karpo稠密层，启动校验拒绝；该失败不包含任何性能请求。",
              "- MXFP4自动后端与SM120不兼容；显式B12X后端又遇到JIT张量维度错误。两项均在启动阶段失败，不能计为加速结果。",
              "- Humming后端实际128个W4A4线性层已通过检查；该量化方案的质量和性能均单独呈现，未以位数下降代替性能证明。",
              f"- 已重算{attempts:,}次请求，其中有效{valid:,}次，失败{attempts-valid:,}次。", ""]
    infrastructure_path = EXP / "results/infrastructure-current.json"
    if infrastructure_path.exists():
        infrastructure = load(infrastructure_path)
        lines += [
            f"- 最新资源快照时间：{infrastructure['utc']}；"
            f"{infrastructure['running_instances']}台EC2运行中，参考计算费用"
            f"{infrastructure['reference_compute_usd_per_hour']:.2f}美元/小时，另计EBS等费用。",
            "- [资源快照](results/infrastructure-current.json) · [结果与待确认扩容方案](DECISION.zh.md)", "",
        ]
    (EXP / "RESULTS.zh.md").write_text("\n".join(lines))
    dump(EXP / "data.json", {"runs": runs, "performance": performance, "quality": quality,
                           "confirmed": confirmed, "verification": verification})
    dump(EXP / "results/verification.json", verification)
    rendered = MarkdownIt("commonmark", {"html": False}).enable("table").render("\n".join(lines))
    rendered = rendered.replace("<table>", '<div class="table-scroll" tabindex="0"><table>').replace(
        "</table>", "</table></div>")
    (EXP / "report.html").write_text(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Karpo 固定RPS优化</title><style>'
        'body{font:16px/1.7 system-ui;margin:32px auto;padding:0 24px;max-width:1500px;'
        'background:#f4f7fb;color:#173044}h1{font-size:clamp(24px,3vw,36px);line-height:1.3}'
        'h2{margin-top:36px;font-size:23px}.table-scroll{overflow-x:auto;background:white;'
        'border:1px solid #d3dfe7;border-radius:10px;margin:20px 0}table{border-collapse:collapse;'
        'width:100%;font-size:14px}th,td{padding:10px 12px;border-bottom:1px solid #dce5eb;'
        'white-space:nowrap;text-align:left}th{background:#e7eff5}tbody tr:nth-child(even)'
        '{background:#f8fafc}a{color:#1766a0}p,li{max-width:1100px}nav{font-size:14px}'
        'code{overflow-wrap:anywhere}@media(max-width:600px){body{padding:0 14px;margin:20px auto}}'
        '@media print{body{background:white}.table-scroll{overflow:visible}table{font-size:9px}'
        'th,td{padding:4px;white-space:normal}}</style></head><body><nav>'
        '<a href="data.json">完整结构化结果</a> · <a href="RESULTS.zh.md">中文报告</a> · '
        '<a href="PLAN.zh.md">验收计划</a> · <a href="DECISION.zh.md">扩容方案</a></nav><main>'
        + rendered + "</main></body></html>"
    )
    print(json.dumps(verification))


if __name__ == "__main__":
    main()
