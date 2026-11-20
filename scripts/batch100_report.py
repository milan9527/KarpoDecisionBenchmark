"""Recompute batch100 evidence and render a separate, portable results report."""
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics

from batch100_benchmark import percentile, summary, validate

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/batch100-20261120"
RESULT = EXP / "results"


def load(path):
    return json.loads(path.read_text())


def number(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def natural_key(value):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(value))]


def verify():
    manifest = load(EXP / "manifest.json")
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    fixtures = {item["id"]: item for scenario in ("risk", "sop")
                for item in load(EXP / f"{scenario}-requests.json")}
    calls = 0
    client_drops = 0
    valid_http_responses = 0
    stages = {}
    errors = []
    for path in sorted(RESULT.glob("*/*/**/requests.jsonl"), key=natural_key):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        stored_path = path.parent / "summary.json"
        if not stored_path.exists():
            continue
        stored = load(stored_path)
        recomputed = summary(rows, stored["elapsed_seconds"])
        for key, value in recomputed.items():
            assert stored[key] == value, (path, key, stored[key], value)
        params = stored["parameters"]
        if params.get("seconds"):
            assert stored["elapsed_seconds"] >= params["seconds"], path
            middle = [r for r in rows if min(10, params["seconds"] / 4)
                      <= r["start_offset_s"] < params["seconds"]]
            if middle:
                assert summary(middle, stored["after_ramp"]["elapsed_seconds"]) == stored["after_ramp"], path
        if stored["mode"] == "open":
            assert len(rows) == int(params["qps"] * params["seconds"])
        elif params.get("count") is not None:
            assert len(rows) == params["count"]
        for row in rows:
            item = fixtures[row["request_id"]]
            if row["valid"]:
                assert row["request_sha256"] == item["request_sha256"]
                assert validate(row["response"], item) == row["correct"]
            if ("quality" in str(path.parent)) and not row["correct"]:
                errors.append({"stage": str(path.parent.relative_to(RESULT)), "request_id": item["id"],
                               "expected": item["expected"], "valid": row["valid"],
                               "actual": row.get("response", {}).get("answers")})
        gpu_counts = Counter(str(r["response"]["metrics"]["gpu"]) for r in rows if r["valid"])
        if "replicas-1" in str(path):
            assert set(gpu_counts) <= {"0"}, path
        if "replicas-2" in str(path) and stored["valid"] >= 100:
            assert set(gpu_counts) == {"0", "1"}, path
        calls += len(rows)
        client_drops += sum(str(r.get("error", "")).startswith("client_inflight_limit_") for r in rows)
        valid_http_responses += sum(r["valid"] for r in rows)
        metrics = [r["response"]["metrics"] for r in rows if r["valid"]]
        timing = {name: {
            "mean": statistics.mean(m[name] for m in metrics),
            "p50": percentile([m[name] for m in metrics], .5),
            "p95": percentile([m[name] for m in metrics], .95)}
            for name in ("queue_ms", "encode_ms", "model_ms")} if metrics else {}
        intervals = [(r["start_offset_s"] + r["send_lag_ms"] / 1000,
                      r["start_offset_s"] + r["latency_ms"] / 1000)
                     for r in rows if r.get("sequence") is not None]
        events = sorted(event for start, end in intervals for event in ((start, 1), (end, -1)))
        inflight = maximum = 0
        for _, change in events:
            inflight += change
            maximum = max(maximum, inflight)
        assert inflight == 0
        concurrency_evidence = {"observed_client_max_inflight": maximum}
        if path.parent.name == "sustained-100":
            assert params["concurrency"] == maximum == 100 and params["seconds"] == 60, path
            concurrency_evidence["mean_client_inflight_seconds_10_to_60"] = sum(
                max(0, min(end, 60) - max(start, 10)) for start, end in intervals) / 50
        stages[str(path.parent.relative_to(RESULT))] = {
            **stored, "gpu_response_counts": dict(gpu_counts), "request_weighted_timing_ms": timing,
            **concurrency_evidence}
    explicit = {}
    explicit_calls = 0
    for path in RESULT.glob("fp8/*/explicit/batches.jsonl"):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        reference = {r["request_id"]: r for r in map(json.loads,
                     (path.parent.parent / "quality-serial/requests.jsonl").read_text().splitlines())}
        computed = []
        for row in rows:
            assert row["http_status"] == 200
            assert len(row["request_ids"]) == len(row["response"]["responses"])
            for identifier, response in zip(row["request_ids"], row["response"]["responses"], strict=True):
                validate(response, fixtures[identifier])
        for batch in (1, 2, 4, 8, 16):
            group = [r for r in rows if r["batch"] == batch]
            same, correct, delta, count = 0, 0, 0, 0
            for row in group:
                for identifier, response in zip(row["request_ids"], row["response"]["responses"], strict=True):
                    actual = response["answers"]["action"]
                    base = reference[identifier]["response"]["answers"]["action"]
                    same += actual["choice"] == base["choice"]
                    correct += validate(response, fixtures[identifier])
                    delta = max(delta, max(abs(p - base["probabilities"][k])
                                           for k, p in actual["probabilities"].items()))
                    count += 1
            computed.append({"batch": batch, "requests": count,
                             "valid_rps": count / sum(r["elapsed_seconds"] for r in group),
                             "batch_p95_ms": percentile([r["elapsed_seconds"] * 1000 for r in group], .95),
                             "same_choices": same, "correct": correct, "max_probability_delta": delta,
                             "max_packs": max(r["response"]["metrics"]["packs"] for r in group),
                             "graph_replays": sum(r["response"]["metrics"]["graph_replays"] for r in group),
                             "peak_allocated_gib": max(r["response"]["metrics"]["peak_allocated_gib"] for r in group)})
        assert computed == load(path.parent / "summary.json"), path
        explicit[path.parent.parent.name] = [
            {**row, "batch_p50_ms": percentile(
                [r["elapsed_seconds"] * 1000 for r in rows if r["batch"] == row["batch"]], .5)}
            for row in computed]
        explicit_calls += sum(len(row["request_ids"]) for row in rows)
    verification = {"verified_single_request_attempts": calls, "http_attempts": calls - client_drops,
                    "client_dropped_requests": client_drops, "valid_http_responses": valid_http_responses,
                    "explicit_model_requests": explicit_calls,
                    "stages": len(stages), "quality_error_observations": len(errors),
                    "status": load(RESULT / "suite-status.json")["status"]}
    if verification["status"] == "complete":
        expected = set()
        for scenario in ("risk", "sop"):
            for phase in ("bf16", "fp8"):
                expected.update(f"{phase}/{scenario}/{name}" for name in ("warmup", "quality-serial"))
            expected.update(f"fp8/{scenario}/tuning-b{batch}-w{wait}"
                            for batch in manifest["batch_sizes"]
                            for wait in ([0] if batch == 1 else manifest["wait_ms"]))
            for replicas in manifest["replicas"]:
                names = ["quality", "burst-100", "sustained-100"]
                names += [f"concurrency-{c}" for c in manifest["concurrency"]]
                names += [f"arrival-{q}" for q in manifest["open_loop_qps"]]
                expected.update(f"fp8/{scenario}/replicas-{replicas}/{name}" for name in names)
        assert set(stages) == expected, (expected - set(stages), set(stages) - expected)
        assert explicit_calls == 320 and set(explicit) == {"risk", "sop"}
        postflight = load(RESULT / "postflight.json")
        assert postflight["gpu_compute_processes"].strip() == ""
        assert postflight["model_weights_bytes"] == 9081227392
    (RESULT / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    (RESULT / "quality-errors.json").write_text(json.dumps(errors, indent=2, ensure_ascii=False) + "\n")
    return stages, explicit, verification


def quality_parity():
    comparisons = {}
    for scenario in ("risk", "sop"):
        base = RESULT / "bf16" / scenario / "quality-serial"
        actual = RESULT / "fp8" / scenario / "quality-serial"
        if not (actual / "parity-vs-reference.json").exists():
            continue
        reference = {r["request_id"]: r for r in map(json.loads, (base / "requests.jsonl").read_text().splitlines())}
        rows = [json.loads(line) for line in (actual / "requests.jsonl").read_text().splitlines()]
        details = []
        recovered = 0
        for row in rows:
            if not row["valid"]:
                details.append({"request_id": row["request_id"], "invalid": True})
                continue
            before = reference[row["request_id"]]
            a, b = before["response"]["answers"]["action"], row["response"]["answers"]["action"]
            recovered += row["correct"] and not before["correct"]
            details.append({"request_id": row["request_id"], "same_choice": a["choice"] == b["choice"],
                            "reference_correct": before["correct"], "actual_correct": row["correct"],
                            "max_probability_delta": max(abs(p - a["probabilities"][k])
                                                         for k, p in b["probabilities"].items())})
        paired = {"observations": len(rows), "same_choices": sum(r.get("same_choice", False) for r in details),
                  "new_errors": sum(r.get("reference_correct", False) and not r.get("actual_correct", False)
                                    for r in details),
                  "max_probability_delta": max((r.get("max_probability_delta", 0) for r in details), default=None),
                  "details": details}
        assert paired == load(actual / "parity-vs-reference.json"), actual
        comparisons[scenario] = {**paired, "recovered_errors": recovered}
    return comparisons


def main():
    stages, explicit, verification = verify()
    state = load(RESULT / "suite-status.json")
    resource = load(RESULT / "infrastructure-final.json") if (RESULT / "infrastructure-final.json").exists() else None
    data = {"verification": verification, "resource": resource, "stages": stages, "explicit": explicit,
            "acceptance": {"concurrency": 100, "latency_metric": "p50_ms", "latency_limit_ms": 500,
                           "source": "ACCEPTANCE.zh.md", "failures_reported_separately": True},
            "precision_comparison": quality_parity(),
            "selection": {s: load(RESULT / "fp8" / s / "selection.json") for s in ("risk", "sop")
                          if (RESULT / "fp8" / s / "selection.json").exists()}}
    data["explicit_first_batch"] = {}
    for path in RESULT.glob("fp8/*/explicit/batches.jsonl"):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        values = {}
        for batch in (1, 2, 4, 8, 16):
            group = [r for r in rows if r["batch"] == batch]
            if group:
                values[str(batch)] = {
                    "first_ms": group[0]["elapsed_seconds"] * 1000,
                    "following_p50_ms": percentile([r["elapsed_seconds"] * 1000 for r in group[1:]], .5)}
        data["explicit_first_batch"][path.parent.parent.name] = values
    outcomes = []
    for scenario, label in [("risk", "风控"), ("sop", "SOP")]:
        key = f"fp8/{scenario}/replicas-2"
        measured, quality = stages.get(key + "/sustained-100"), stages.get(key + "/quality")
        if measured and quality:
            passed = measured["failures"] == 0 and measured["p50_ms"] <= 500
            outcomes.append(f"{label}：两卡持续100并发P50 {number(measured['p50_ms'])}ms"
                            f"（P95 {number(measured['p95_ms'])}ms），"
                            f"有效且500ms内 {measured['valid_within_500ms']}/{measured['attempts']}；"
                            f"合成质量观察 {quality['correct']}/{quality['attempts']}。"
                            f"本次模型服务延迟目标{'达到' if passed else '未达到'}。")
    if not outcomes:
        outcomes = ["实验执行中，尚无完整的两卡100并发结论。"]
    lines = [
        "# Karpo 4B：风控与SOP批处理、100并发实测",
        "",
        "两场景独立测试。风控为200个输入特征、6个处置动作；SOP为完整200候选选一。",
        "硬件：同一台g7e.12xlarge，两张RTX PRO 6000 Blackwell 96GB；每GPU完整4B副本。",
        "后端：实验性Karpo原生动态批处理，FP8、多序列CUDA Graph；本轮没有vLLM性能结果。",
        "最新验收：**100并发，P50≤500ms**；P95/P99仅作观察。"
        "[用户澄清及口径优先级](ACCEPTANCE.zh.md)。",
        "",
        *outcomes,
        "",
        "**边界**：客户端与模型同机，计时到完整HTTP响应，包含编码、排队及推理；未包含生产网络和特征查询。"
        "风控是合成规则执行任务，SOP也是合成案例；这些质量结果不能证明客户生产准确率。",
        "",
        "## FP8与BF16质量对照",
        "",
        "BF16仅作为已经完成的单请求参考；后续批处理、并发与到达速率测试全部使用FP8。"
        "总体一致率接近仍可能存在错误迁移，所以下表同时列出逐条选择差异与新增错误。",
        "",
        "| 场景 | BF16正确/观察 | FP8正确/观察 | 选择相同 | FP8新增错误 | FP8修复错误 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for scenario, label in [("risk", "风控"), ("sop", "SOP")]:
        paired = data["precision_comparison"].get(scenario)
        if not paired:
            continue
        bf, fp = [stages[f"{phase}/{scenario}/quality-serial"] for phase in ("bf16", "fp8")]
        lines.append(f"| {label} | {bf['correct']}/{bf['attempts']} | {fp['correct']}/{fp['attempts']} | "
                     f"{paired['same_choices']}/{paired['observations']} | {paired['new_errors']} | "
                     f"{paired['recovered_errors']} |")
    lines += [
        "",
        "此处同时改变了精度和执行路径（BF16无Graph、FP8启用Graph），差异不能全部归因于量化。"
        "FP8降低部分权重存储和矩阵运算成本，但实际显存还包含未量化层、激活及Graph缓冲；不能按位宽直接认定整机资源减半。",
        "",
        "## 100并发与质量",
        "",
        "| 场景 | GPU副本 | 质量正确/观察 | 100并发有效/尝试 | 请求/秒（含排空） | P50 ms（验收） | P95 ms | P99 ms | 有效且≤500ms/全部尝试 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario, label in [("risk", "风控"), ("sop", "SOP")]:
        for replica in (1, 2):
            key = f"fp8/{scenario}/replicas-{replica}"
            q, s = stages.get(key + "/quality"), stages.get(key + "/sustained-100")
            if not q or not s:
                continue
            lines.append(f"| {label} | {replica} | {q['correct']}/{q['attempts']} | "
                         f"{s['valid']}/{s['attempts']} | {number(s['valid_rps'], 2)} | "
                         f"{number(s['p50_ms'])} | {number(s['p95_ms'])} | {number(s['p99_ms'])} | "
                         f"{s['valid_within_500ms']}/{s['attempts']} |")
    lines += ["", "持续投递60秒，达到时间后排空；100个客户端工作协程无思考时间。P50/P95/P99针对有效响应，"
              "失败及500ms内比例使用全部尝试分母。闭环吞吐包含起步和排空，不等同固定到达速率。",
              "已由逐条调用起止时间重建在途数，四组持续测试的实际客户端峰值均为100；"
              "预热后10–60秒窗口的平均在途数见data.json。",
              "", "服务端阶段耗时按请求加权；同一batch的请求共享编码和模型时间，分项分位数不可直接相加。", ""]
    for scenario, label in [("risk", "风控"), ("sop", "SOP")]:
        row = stages.get(f"fp8/{scenario}/replicas-2/sustained-100")
        if row:
            t = row["request_weighted_timing_ms"]
            lines.append(f"- {label}两卡：排队P50 {number(t['queue_ms']['p50'])}ms，"
                         f"编码P50 {number(t['encode_ms']['p50'])}ms，"
                         f"模型P50 {number(t['model_ms']['p50'])}ms。")
    lines += [
        "", "## 已测P50≤500ms范围", "",
        "| 场景 | GPU副本 | 扫描中最大达标并发 | 固定到达中最大达标请求/秒 |",
        "|---|---:|---:|---:|"]
    capacity = {}
    for scenario, label in [("risk", "风控"), ("sop", "SOP")]:
        for replica in (1, 2):
            prefix = f"fp8/{scenario}/replicas-{replica}/"
            passing = [key for key, row in stages.items() if key.startswith(prefix)
                       and row["failures"] == 0 and row["p50_ms"] is not None and row["p50_ms"] <= 500]
            concurrency = max((int(key.rsplit("-", 1)[-1]) for key in passing if "/concurrency-" in key),
                              default=None)
            arrival = max((int(key.rsplit("-", 1)[-1]) for key in passing if "/arrival-" in key), default=None)
            capacity[f"{scenario}/{replica}"] = {"max_tested_concurrency": concurrency,
                                                "max_tested_arrival_qps": arrival}
            lines.append(f"| {label} | {replica} | {number(concurrency, 0)} | {number(arrival, 0)} |")
    data["observed_500ms_range"] = capacity
    lines += [
        "", "达标定义为该档零失败且完整HTTP响应P50≤500ms。采用本次吞吐优先选中的配置；扫描档位有限、固定到达每档仅10秒，"
        "此表不是精确容量上限或长期SLA。100个在线连接与100个持续在途请求的需求不同。",
        "", "## 批大小与推理一致性", "",
              "| 场景 | batch | 请求/秒 | batch P50 ms | batch P95 ms | 与FP8单请求选择相同 | 最大概率差 | 最大pack数 | Graph重放次数 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for scenario, values in explicit.items():
        for row in values:
            lines.append(f"| {scenario} | {row['batch']} | {number(row['valid_rps'], 2)} | "
                         f"{number(row['batch_p50_ms'])} | {number(row['batch_p95_ms'])} | {row['same_choices']}/{row['requests']} | "
                         f"{row['max_probability_delta']:.6f} | {row['max_packs']} | {row['graph_replays']} |")
    lines += ["", "每档32条分散抽取的质量观察，不是额外独立案例。显式batch延迟覆盖整批完成，"
              "每档没有额外剔除首批成本；主并发测试在显式扫描和调优之后。"
              "单pack总token预算65536；超过时拆pack。"]
    for scenario, values in data["explicit_first_batch"].items():
        for batch, value in values.items():
            subsequent = value["following_p50_ms"]
            if subsequent and value["first_ms"] > subsequent * 2:
                lines.append(f"{scenario} batch={batch}首批{number(value['first_ms'])}ms，"
                             f"其余批次P50 {number(subsequent)}ms；首批观测保留在表内统计中。")
    lines += ["", "## 选中动态配置", ""]
    for scenario, selection in data["selection"].items():
        c = selection["selected"]
        lines.append(f"- {scenario}：max_batch={c['batch']}，wait={c['wait_ms']}ms；"
                     f"32并发调优吞吐{number(c['summary']['valid_rps'], 2)}请求/秒。")
    lines += ["", "选择规则：有效调优档中吞吐最高，平局取P95最低；不以质量标签选速度配置。"
              "SOP调优会重复原有质量输入；风控调优60个状态与质量120个状态分离。", "",
              "该选择规则优先吞吐，不等同最优延迟配置。SOP各档吞吐差距很小；生产调参应加上P50约束，"
              "不因大batch略快就直接采用。", "",
              "## 固定到达速率", "",
              "| 场景 | 副本 | 计划请求/秒 | 有效/计划请求 | 完整窗口+排空请求/秒 | P50 ms | P95 ms | 有效且≤500ms |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key, row in stages.items():
        if "/arrival-" not in key:
            continue
        _, scenario, replica, name = key.split("/")
        lines.append(f"| {scenario} | {replica.split('-')[-1]} | {name.split('-')[-1]} | "
                     f"{row['valid']}/{row['attempts']} | {number(row['valid_rps'], 2)} | "
                     f"{number(row['p50_ms'])} | {number(row['p95_ms'])} | {row['valid_within_500ms']}/{row['attempts']} |")
    lines += ["", "固定到达每档10秒，客户端上限256在途；超限仍记录为失败。计时从计划到达开始，包含客户端发送延误。"
              "短窗口不能作为长期SLA。所有扫描、突发、BF16/FP8与批处理一致性明细见data.json及results。",
              "", "## 部署判断", "",
              "FP8作为后续性能验证的默认精度，保留已完成的BF16参考。模型版本、量化方法或输入分布变化时，"
              "再用固定质量集做小规模回归，无需每次重复BF16全套压力测试。",
              "",
              "本轮只实测Karpo原生服务改造。vLLM已有结构化决策模型及Qwen3.5相关支持，"
              "但不能把Karpo当普通聊天模型加载后直接认定结果等价：还需核对双向注意力、输入模板、"
              "决策标记、readout权重与答案代码映射，并做逐条质量和并发回归。"
              "[vLLM结构化决策文档](https://docs.vllm.ai/en/latest/models/structured_decisions_models/)；"
              "本报告不承诺换用vLLM即可达到100并发。",
              "Karpo对完整输入做前向计算后直接选择答案。其双向注意力不能直接套用普通因果生成模型的前缀KV复用，"
              "因此应比较实际输入的前向吞吐，而不能仅凭引擎支持100条序列就判定延迟达标。",
              "",
              "风控生产验收需使用客户标注数据，分别检查恶意行为漏报、正常用户误杀和高损失动作错误。"
              "配额、幂等性和限流适合在服务侧确定性执行；若主要是结构化特征分类，可另测规则或树模型主链路，"
              "由Karpo补充复杂语义判断。本轮没有测这类替代架构的性能或准确率。",
              "", "## 证据与资源", "",
              f"运行状态：{state['status']}。核验单请求计划/尝试记录{verification['verified_single_request_attempts']}条，"
              f"其中HTTP调用尝试{verification['http_attempts']}次、客户端超限未发出{verification['client_dropped_requests']}次；"
              f"另有显式batch内请求{verification['explicit_model_requests']}次。",
              "",
              "[计划](PLAN.zh.md) · [机器可读汇总](data.json) · [核验](results/verification.json) · "
              "[质量错误](results/quality-errors.json) · [执行版本](manifest.json)"]
    if resource:
        lines += ["", f"实例状态：**{resource['state']}**；核对时间：{resource['observed_utc']}。"
                  "没有terminate；模型和EBS保留。"]
    markdown = "\n".join(lines) + "\n"
    (EXP / "RESULTS.zh.md").write_text(markdown)
    # Minimal self-contained HTML, with sortable-by-view section selection and raw data download.
    tables = []
    for scenario, label in [("risk", "风控 · 200特征"), ("sop", "SOP · 200候选")]:
        table = ['<table><thead><tr><th>阶段</th><th>有效/尝试</th><th>正确/尝试</th><th>请求/秒</th>'
                 '<th>P50</th><th>P95</th><th>P99</th><th>≤500ms/尝试</th></tr></thead><tbody>']
        for key, row in stages.items():
            if f"/{scenario}/" not in key:
                continue
            bad = row["failures"] > 0 or (row["p50_ms"] or math.inf) > 500
            table.append(f'<tr class="{"bad" if bad else "good"}"><td>{html.escape(key)}</td>'
                         f'<td>{row["valid"]}/{row["attempts"]}</td><td>{row["correct"]}/{row["attempts"]}</td>'
                         f'<td>{number(row["valid_rps"],2)}</td><td>{number(row["p50_ms"])}</td>'
                         f'<td>{number(row["p95_ms"])}</td><td>{number(row["p99_ms"])}</td>'
                         f'<td>{row["valid_within_500ms"]}/{row["attempts"]}</td></tr>')
        table.append("</tbody></table>")
        tables.append(f'<section id="{scenario}"><h2>{label}</h2><div class="scroll">{"".join(table)}</div></section>')
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Karpo 4B · 两场景100并发验证</title><style>
body{font:16px/1.6 system-ui,sans-serif;margin:32px auto;max-width:1400px;padding:0 24px;color:#17243a;background:#f7f9fc;overflow-wrap:anywhere}
h1{font-size:30px}a{color:#245abe}.note{padding:16px;background:#fff4d4;border-radius:10px}.scroll{overflow:auto}
.outcome{padding:16px;background:white;border-left:4px solid #245abe;border-radius:6px}
table{border-collapse:collapse;width:100%;background:white;font-size:14px}th,td{padding:9px;border-bottom:1px solid #dde3eb;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.bad td:nth-child(5){color:#ac3030}.good td:nth-child(5){color:#147343}
nav{display:flex;flex-wrap:wrap;gap:20px;margin:20px 0}section{margin:32px 0}pre{white-space:pre-wrap;background:white;padding:20px}
@media print{body{margin:0}nav{display:none}.scroll{overflow:visible}table{font-size:9px}}
</style><h1>Karpo 4B · 两场景100并发验证</h1>
<p>两张 RTX PRO 6000 Blackwell 96GB · Karpo原生动态批处理 · FP8 + CUDA Graph</p>
<p class="note">最新验收：100并发，P50≤500ms；P95/P99仅作观察。同机完整HTTP延迟，含编码、排队、推理，不含生产网络和特征查询。风控合成规则一致率与SOP合成案例一致率均不代表生产准确率。红色P50表示超过500ms或该档有失败。失败始终计入尝试分母。</p>
<nav><a href="#risk">风控</a><a href="#sop">SOP</a><a href="RESULTS.zh.md">文字报告</a><a href="data.json" download>下载汇总JSON</a><a href="ACCEPTANCE.zh.md">验收口径</a><a href="PLAN.zh.md">实验计划</a></nav>
"""
    document += "".join(f'<p class="outcome">{html.escape(text)}</p>' for text in outcomes)
    document += "".join(tables)
    document += f"<details><summary>完整文字报告与口径</summary><pre>{html.escape(markdown)}</pre></details></html>"
    (EXP / "report.html").write_text(document)
    (EXP / "data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [*RESULT.rglob("*"), EXP / "RESULTS.zh.md", EXP / "report.html", EXP / "data.json",
                        EXP / "ACCEPTANCE.zh.md", ROOT / "README.md",
                        Path(__file__).resolve()]
              if p.is_file() and p.name != "evidence-sha256.json" and p.suffix != ".tmp"}
    (RESULT / "evidence-sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(json.dumps(verification, indent=2))


if __name__ == "__main__":
    main()
