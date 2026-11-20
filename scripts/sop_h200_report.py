"""Recompute H200 SOP measurements and publish a self-contained local report."""
import csv
import datetime as dt
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics

from benchmark import validate
from vllm_rps_benchmark import summarize

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-sop-h200-20261120"


def load(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def inline(text):
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def main():
    items = {r["id"]: r for r in load(EXP / "sop-requests.json")}
    encodings = {r["request_id"]: r for r in load(EXP / "encoding-reference.json")}
    for name in ("sop-requests.json", "encoding-reference.json"):
        assert (EXP / name).read_bytes() == (
            ROOT / "experiments/vllm-rps100-20261120" / name).read_bytes()
    assert len(items) == 104
    assert all(
        item["candidate_count"] == 200
        and len(item["request"]["questions"]["action"]["criteria"]) == 200
        for item in items.values()
    )
    baseline_config = load(
        ROOT / "experiments/vllm-rps100-20261120/results/runs/single-fleetnode-v1/b4/server-0/engine-config.json")
    original_readout_hash = baseline_config["workers"][0]["readout_sha256"]
    quality_baseline_path = (
        ROOT / "experiments/vllm-rps100-20261120/results/runs/rtx4-fp8-v1/b4/r4/sop/quality/requests.jsonl"
    )
    quality_baseline = {r["request_id"]: r for r in map(json.loads, quality_baseline_path.open())}
    stages, runs, workers = [], [], []
    verified = valid_total = correct_total = 0
    for run in sorted((EXP / "results/runs").iterdir()):
        if not (run / "status.json").exists():
            continue
        status = load(run / "status.json")
        manifest = load(run / "manifest.json")
        for name, expected in manifest["files"].items():
            frozen = EXP / "frozen" / manifest["revision"] / name
            assert frozen.is_file() and hashlib.sha256(frozen.read_bytes()).hexdigest() == expected, name
        hardware = list(csv.DictReader((run / "gpu-hardware.csv").open(), skipinitialspace=True))
        assert len(hardware) == 8 and all("H200" in r["name"] for r in hardware)
        for config_path in sorted(run.glob("*/b*/server-*/engine-config.json")):
            config = load(config_path)
            assert config["settings"]["tensor_parallel_size"] == 1
            assert config["settings"]["quantization"] == "fp8_per_channel"
            assert config["settings"]["enable_prefix_caching"] is False
            assert config["full_attention_is_causal"] is False
            for worker in config["workers"]:
                assert worker["readout_dtype"] == "torch.float32"
                assert worker["readout_sha256"] == original_readout_hash
                assert all("ENCODER_ONLY" in r["type"].upper() for r in worker["attention"])
                assert any("float8" in dtype for dtype in worker["parameter_elements_by_dtype"])
            workers.append({"source": str(config_path.relative_to(EXP)), "workers": config["workers"]})
        gpu_rows = []
        for row in list(csv.reader((run / "gpu.csv").open()))[1:]:
            if len(row) != 5:
                continue
            timestamp = dt.datetime.strptime(row[0], "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
            gpu_rows.append((timestamp, int(row[1]), *[float(row[i].split()[0]) for i in (2, 3, 4)]))
        run_stages = []
        for entry in status["stages"]:
            phase, batch, replicas, stage = (entry[k] for k in ("phase", "batch", "replicas", "stage"))
            directory = run / phase / f"b{batch}/r{replicas}/sop" / stage
            rows = [json.loads(line) for line in (directory / "requests.jsonl").open()]
            summary = load(directory / "summary.json")
            assert len(rows) == summary["attempts"]
            assert sorted(r["sequence"] for r in rows) == list(range(len(rows)))
            for row in rows:
                item = items[row["request_id"]]
                assert row["request_sha256"] == item["request_sha256"]
                assert row["expected"] == item["expected"]
                assert math.isclose(row["latency_ms"],
                                    (row["end_offset_s"] - row["scheduled_offset_s"]) * 1000, abs_tol=1e-5)
                assert math.isclose(row["http_latency_ms"],
                                    (row["end_offset_s"] - row["send_offset_s"]) * 1000, abs_tol=1e-5)
                if row["valid"]:
                    assert row["http_status"] == 200
                    response = row["response"]
                    assert row["correct"] == validate(response, item)
                    expected = encodings[item["id"]]
                    assert response["usage"]["input_tokens"] == expected["length"]
                    metrics = response["metrics"]
                    assert metrics["tokens_sha256"] == expected["tokens_sha256"]
                    assert metrics["marker"] == expected["marker"]
                    assert metrics["max_num_seqs"] == batch
                    assert metrics["replica_id"] == metrics["proxy_replica"]
                    assert 0 <= metrics["replica_id"] < replicas
                else:
                    assert not row["correct"]
            if stage == "quality":
                assert len(rows) == 104 and len({r["request_id"] for r in rows}) == 104
                rebuilt = {
                    "attempts": len(rows), "valid": sum(r["valid"] for r in rows),
                    "correct": sum(r["correct"] for r in rows),
                }
                rebuilt["failures"] = rebuilt["attempts"] - rebuilt["valid"]
                rebuilt["accuracy"] = rebuilt["correct"] / rebuilt["attempts"]
            else:
                rebuilt = summarize(rows, summary["target_rps"], summary["offered_seconds"],
                                    summary["elapsed_with_drain_s"])
            assert rebuilt == summary, str(directory)
            assert entry["result"] == summary
            timing = load(directory / "timing.json")
            begin = dt.datetime.fromisoformat(timing["begin_utc"])
            if stage == "quality":
                end = dt.datetime.fromisoformat(timing["end_utc"])
                sample_begin = begin
            else:
                end = dt.datetime.fromisoformat(timing["offered_end_utc"])
                sample_begin = begin + dt.timedelta(seconds=summary["offered_seconds"] / 4)
            gpu = []
            for replica in range(replicas):
                samples = [r for r in gpu_rows if r[1] == replica and sample_begin <= r[0] < end]
                if samples:
                    gpu.append({
                        "gpu": replica, "samples": len(samples),
                        "utilization_mean_pct": statistics.mean(r[3] for r in samples),
                        "memory_max_gib": max(r[2] for r in samples) / 1024,
                        "power_mean_w": statistics.mean(r[4] for r in samples),
                    })
            record = {
                "run": run.name, "phase": phase, "batch": batch, "replicas": replicas, "stage": stage,
                "source": str(directory.relative_to(EXP)), "summary": rebuilt, "timing": timing,
                "gpu_steady_samples": gpu,
            }
            if stage == "quality":
                assert set(quality_baseline) == {r["request_id"] for r in rows}
                record["rtx_quality_comparison"] = {
                    "source": str(quality_baseline_path.relative_to(ROOT)),
                    "choice_matches": sum(
                        r["valid"] and r["response"]["answers"]["action"]["choice"]
                        == quality_baseline[r["request_id"]]["response"]["answers"]["action"]["choice"]
                        for r in rows
                    ),
                    "attempts": len(rows),
                }
            stages.append(record)
            run_stages.append(record)
            verified += len(rows)
            valid_total += sum(r["valid"] for r in rows)
            correct_total += sum(r["correct"] for r in rows)
        confirmation = [r for r in run_stages if r["phase"] == "confirmation" and r["stage"] in ("confirm", "repeat")]
        quality = [r for r in run_stages if r["phase"] == "confirmation" and r["stage"] == "quality"]
        candidates = []
        for entry in run_stages:
            if entry["phase"] != "sweep" or entry["stage"] != "rps100":
                continue
            s = entry["summary"]
            q = next(
                x["summary"] for x in run_stages
                if x["phase"] == "sweep" and x["stage"] == "quality"
                and x["batch"] == entry["batch"] and x["replicas"] == entry["replicas"]
            )
            if (not q["failures"] and q["accuracy"] > .9 and s["passes_probe"]
                    and s["correct"] / s["attempts"] > .9):
                candidates.append({
                    "batch": entry["batch"], "replicas": entry["replicas"], "probe_p50_ms": s["p50_ms"],
                })
        assert candidates == status["candidates"]
        if status.get("selected"):
            assert status["selected"] == min(candidates, key=lambda x: (x["replicas"], x["probe_p50_ms"]))
            assert all(
                entry["batch"] == status["selected"]["batch"]
                and entry["replicas"] == status["selected"]["replicas"]
                for entry in confirmation + quality
            )
        confirmed = bool(
            len(confirmation) == 2 and len(quality) == 1
            and quality[0]["summary"]["accuracy"] > .9 and not quality[0]["summary"]["failures"]
            and all(r["summary"]["passes_performance_acceptance"]
                    and r["summary"]["correct"] / r["summary"]["attempts"] > .9 for r in confirmation)
        )
        assert confirmed == status["confirmed"]
        runs.append({"name": run.name, "status": status["status"], "confirmed": confirmed,
                     "selected": status.get("selected"), "error": status.get("error")})
    verdict = any(r["confirmed"] for r in runs)
    instance = load(EXP / "results/instance.json")
    pricing_path = EXP / "results/pricing-final.json"
    pricing = load(pricing_path) if pricing_path.exists() else None
    lifecycle_path = ROOT / "operations/stop-20261120/infrastructure-stopped.json"
    lifecycle = None
    if lifecycle_path.exists():
        snapshot = load(lifecycle_path)
        assert snapshot["all_stopped"]
        lifecycle = {
            "source": str(lifecycle_path.relative_to(ROOT)), "utc": snapshot["utc"],
            "all_stopped": True, "instance_count": snapshot["instance_count"],
        }
    verification = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "verified_attempts": verified,
        "valid": valid_total, "failures": verified - valid_total, "correct": correct_total,
        "verified_stages": len(stages), "confirmed": verdict,
    }
    data = {"verification": verification, "instance": instance, "pricing": pricing, "lifecycle": lifecycle,
            "runs": runs, "stages": stages, "workers": workers}
    write(EXP / "results/verification.json", verification)
    write(EXP / "data.json", data)
    lines = [
        "# H200 SOP验证结果", "",
        f"结论：**{'通过两轮正式验收' if verdict else '尚未通过两轮正式验收'}**。核对时间：{verification['utc']}。",
        "", "完整200候选、原Karpo 4B、vLLM FP8；每卡独立模型副本。全部测试GPU均为H200。",
        "固定100 RPS与端到端P50≤500ms验收包含本机完整HTTP、代理、排队、编码和推理；生产入口及跨区域链路未包含。",
        "质量与压力阶段均使用现有104条SOP合成样例，压力阶段循环复用；不是两个独立数据集，也不代表生产准确率。",
    ]
    if lifecycle:
        lines += [
            "", f"**资源状态已更新：截至{lifecycle['utc']}，本项目全部"
            f"{lifecycle['instance_count']}台EC2均已stopped。** 按用户新指令停止，实例及EBS保留，"
            "实例计算计费已停止，保留的EBS与快照等仍可能计费。",
            "[停机核对记录](../../operations/stop-20261120/infrastructure-stopped.json)。",
        ]
    if verdict:
        confirmed_runs = {r["name"] for r in runs if r["confirmed"]}
        finals = [s for s in stages if s["run"] in confirmed_runs
                  and s["phase"] == "confirmation" and s["stage"] in ("confirm", "repeat")]
        selected = next(r["selected"] for r in runs if r["confirmed"])
        lines += [
            "", f"正式配置：**{selected['replicas']}张H200、每卡一个完整模型副本、最大活跃序列数"
            f"{selected['batch']}**。两轮各120秒，合计"
            f"{sum(s['summary']['attempts'] for s in finals):,}个请求，全部有效。",
            "", "| 正式轮次 | 固定到达率 | P50 ms | P95 ms | 稳态有效RPS | 正确/全部 | 失败 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for entry in finals:
            s = entry["summary"]
            lines.append(
                f"| {entry['stage']} | {s['target_rps']:g} | {s['p50_ms']:.2f} | {s['p95_ms']:.2f} | "
                f"{s['steady_valid_rps']:.2f} | {s['correct']}/{s['attempts']} "
                f"({s['correct']/s['attempts']:.2%}) | {s['failures']} |")
        lines += [
            "", "1／2／4／8卡扫描中，仅8卡通过100 RPS探测；没有测试5／6／7卡，因此不能声称8卡是绝对最少卡数。"
            "batch=1与4的八卡探测P50非常接近，按冻结规则选择较低者，不据此宣称batch=4有稳定速度优势。",
        ]
    lines += [
        "", "## 性能", "",
        "| 运行 | 阶段 | 最大序列数 | GPU副本 | 目标RPS | 秒数 | 有效/全部 | 稳态RPS | P50 ms | P95 ms | 正确/全部 | 门槛 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for entry in stages:
        s = entry["summary"]
        if entry["stage"] == "quality":
            continue
        p50 = f"{s['p50_ms']:.2f}" if s["p50_ms"] is not None else "无有效响应"
        p95 = f"{s['p95_ms']:.2f}" if s["p95_ms"] is not None else "无有效响应"
        passed = s["passes_probe"] and s["correct"] / s["attempts"] > .9
        lines.append(
            f"| {entry['run']} | {entry['phase']}/{entry['stage']} | {entry['batch']} | {entry['replicas']} | "
            f"{s['target_rps']:g} | {s['offered_seconds']:g} | {s['valid']}/{s['attempts']} | "
            f"{s['steady_valid_rps']:.2f} | {p50} | {p95} | {s['correct']}/{s['attempts']} | "
            f"{'通过该阶段' if passed else '未通过'} |")
    lines += ["", "20秒探测及低负载通过均不能替代两轮120秒100 RPS确认。失败请求全部计入验收。", "",
              "## 质量", "",
              "| 运行/阶段 | 最大序列数 | GPU副本 | 正确/全部 | 正确率 | 无效请求 | 与RTX选择相同 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for entry in stages:
        if entry["stage"] != "quality":
            continue
        s = entry["summary"]
        comparison = entry["rtx_quality_comparison"]
        lines.append(f"| {entry['run']}/{entry['phase']} | {entry['batch']} | {entry['replicas']} | "
                     f"{s['correct']}/{s['attempts']} | {s['accuracy']:.2%} | {s['failures']} | "
                     f"{comparison['choice_matches']}/{comparison['attempts']} |")
    lines += [
        "", "## GPU采样", "",
        "每2秒采样一次；以下取各100 RPS阶段后75%的到达窗口，排除启动和排空阶段。"
        "利用率、功耗先按每张活跃GPU求均值，再列各卡均值范围；显存含vLLM预分配。",
        "", "| 阶段 | 最大序列数 | GPU副本 | 各卡利用率均值 | 单卡最高显存GiB | 各卡功耗均值W |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for entry in stages:
        if entry["stage"] not in ("rps100", "confirm", "repeat") or not entry["gpu_steady_samples"]:
            continue
        samples = entry["gpu_steady_samples"]
        utilization = [g["utilization_mean_pct"] for g in samples]
        power = [g["power_mean_w"] for g in samples]
        lines.append(
            f"| {entry['run']}/{entry['phase']}/{entry['stage']} | {entry['batch']} | {entry['replicas']} | "
            f"{min(utilization):.1f}–{max(utilization):.1f}% | "
            f"{max(g['memory_max_gib'] for g in samples):.1f} | {min(power):.0f}–{max(power):.0f} |")
    lines += [
        "", "## 资源与费用", "",
        f"实例：{instance['InstanceType']}，8张H200，{instance['Placement']['AvailabilityZone']}。"
        f"启动时Spot参考价${instance['reference_usd_per_hour']:.4f}/小时，按需参考价"
        f"${instance['on_demand_usd_per_hour']:.3f}/小时。",
        "运行时即使某阶段只启用1／2／4张GPU，仍按整台8卡实例计费；不含旧测试资源、EBS、网络、IPv4等。"
        + ("当前EC2已停止，以下为重新运行时的参考费用。" if lifecycle else "测试结束时EC2暂时保留运行。"),
    ]
    if pricing:
        hours = pricing["month_hours"]
        lines += [
            "", f"价格复核时间：{pricing['utc']}。按{hours}小时/月，仅本台八卡H200的计算费用：",
            "", "| 计费方式 | 美元/小时 | 美元/月 |", "|---|---:|---:|",
            f"| Spot参考价 | {pricing['spot_usd_per_hour']:.4f} | "
            f"{pricing['spot_usd_per_hour']*hours:,.2f} |",
            f"| 按需 | {pricing['on_demand_usd_per_hour']:.3f} | "
            f"{pricing['on_demand_usd_per_hour']*hours:,.2f} |",
            "", "Spot价格会变化。报价原始记录见[价格快照](results/pricing-final.json)。",
        ]
    lines += [
        "", "## 复核与复现", "",
        f"逐条核验{verified:,}次请求，{valid_total:,}次有效，{verified-valid_total:,}次失败；全部历史过载阶段也保留。",
        "所有有效请求的原始输入编码、token数、marker与RTX基线一致；FP32输出头哈希一致，"
        "双向全注意力保持，前缀缓存关闭。没有裁剪200候选或缓存决策答案。",
        "v1部署在CPU适配器测试时发现打包缺少辅助脚本，未开始模型性能测量；补齐依赖后冻结v2重新执行，"
        "六项CPU测试通过，八张H200的CUDA计算检查通过。失败的v1安装日志保留。",
        "重新核验并生成报告：`python scripts/sop_h200_report.py`。运行源码与输入冻结在`frozen/v2/`，"
        "服务启动参数、版本和逐条请求保存在`results/runs/h200-fp8-v2/`。",
        "", "[完整数据及GPU采样统计](data.json) · [核验记录](results/verification.json) · "
        "[冻结计划](PLAN.zh.md) · [复现说明](REPRODUCE.zh.md)",
    ]
    if lifecycle:
        lines += [
            "", "本页及HTML已按停机指令更新。原始`evidence-manifest.json`和`backup.json`对应Git提交"
            "`ccf3c30`时的测试封存；修改前的文档与生成器另存于"
            "`operations/stop-20261120/previous/`，路径相对于仓库根目录。逐条测试结果及冻结运行源码未改动。",
        ]
    markdown = "\n".join(lines) + "\n"
    (EXP / "RESULTS.zh.md").write_text(markdown)
    rendered, table_rows = [], []

    def flush_table():
        if table_rows:
            header, *body = table_rows
            rendered.append("<div class='table'><table><thead><tr>" + "".join(
                f"<th>{html.escape(x)}</th>" for x in header) + "</tr></thead><tbody>" + "".join(
                "<tr>" + "".join(f"<td>{html.escape(x)}</td>" for x in row) + "</tr>" for row in body
            ) + "</tbody></table></div>")
            table_rows.clear()

    for line in lines:
        if line.startswith("|"):
            if not line.startswith("|---"):
                table_rows.append([x.strip() for x in line.strip("|").split("|")])
            continue
        flush_table()
        if line.startswith("# "):
            rendered.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            rendered.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line:
            rendered.append(f"<p>{inline(line)}</p>")
    flush_table()
    html_report = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>H200 SOP验证结果</title><style>
body{font-family:system-ui,sans-serif;max-width:1280px;margin:40px auto;padding:0 24px;color:#172338;line-height:1.7}
h1,h2{line-height:1.3}.table{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:10px;border-bottom:1px solid #dbe3eb;text-align:left;white-space:nowrap}th{background:#eef4f8}
a{color:#145dad}</style><body>""" + "".join(rendered) + (
        '<p><a href="data.json">完整数据</a> · <a href="RESULTS.zh.md">Markdown报告</a> · '
        '<a href="results/verification.json">核验记录</a> · '
        '<a href="REPRODUCE.zh.md">复现说明</a></p></body></html>')
    (EXP / "report.html").write_text(html_report)
    print(json.dumps(verification))


if __name__ == "__main__":
    main()
