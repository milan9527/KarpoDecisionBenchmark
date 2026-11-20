"""Verify completed family measurements and render the latest full-200 report."""
import collections
import csv
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re

from benchmark import summarize, validate
from decision_example import load_decision_example, decision_example_html, decision_example_markdown
from prepare import digest
from report_common import documentation_metadata, table
from replica_report import verify as verify_replicas

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/family-20261120"
QWEN = ROOT / "experiments/qwen-retry-20261120"
PREVIOUS = ROOT / "experiments/concise-fp8-20261119"
CANDIDATE_EXAMPLE_NOTE = (
    "以下8项摘自本次合成测试的同一份200候选列表。每项包含候选ID、业务域、动作和适用条件。"
    "英文列为实际输入原文；中文仅作阅读说明。实测每次完整传入200项，由模型选择一个ID。"
)
CANDIDATE_EXAMPLE_HEADERS = ["候选 ID", "实际候选描述（英文精简原文）", "中文说明"]


def load(path):
    return json.loads(path.read_text())


def fmt(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def choice(row):
    return row.get("response", {}).get("answers", {}).get("action", {}).get("choice")


def load_candidate_examples():
    requests = load(EXP / "requests.json")
    candidates = requests[0]["request"]["questions"]["action"]["criteria"]
    assert len(candidates) == 200
    assert all(r["request"]["questions"]["action"]["criteria"] == candidates for r in requests)
    meanings = [
        ("sop_178", "用户间转账：账户操作要求身份验证，且验证尚未完成时，先核验身份。"),
        ("sop_173", "账户及登录：存在合规冻结时，合规审核优先于审批和客户继续执行的要求。"),
        ("sop_048", "加密资产提现：同一幂等键对应的操作已经执行，应防止重复操作。"),
        ("sop_063", "币种兑换：同一幂等键对应的操作已经执行，应防止重复操作。"),
        ("sop_163", "商户退款：申请在允许期限内，且尚未执行退款时，进入退款审核。"),
        ("sop_167", "商户退款：外部已确认完成，但内部账本金额不一致时，核对账本。"),
        ("sop_198", "加密资产充值：外部已确认完成，但内部账本金额不一致时，核对账本。"),
        ("sop_031", "加密资产充值：超过处理时限仍待处理，且无重复执行、无合规冻结时，调查延迟。"),
    ]
    examples = [{"id": key, "description": candidates[key], "meaning_zh": meaning}
                for key, meaning in meanings]
    return candidates, examples


def candidate_examples_markdown(examples, catalog_link):
    rows = [CANDIDATE_EXAMPLE_HEADERS, ["---"] * 3]
    rows += [[e["id"], e["description"], e["meaning_zh"]] for e in examples]
    table_lines = ["| " + " | ".join(value.replace("|", r"\|") for value in row) + " |" for row in rows]
    return "\n\n".join([
        CANDIDATE_EXAMPLE_NOTE,
        "\n".join(table_lines),
        f"[查看完整200候选 JSON]({catalog_link})",
    ])


def verify():
    manifest = load(EXP / "manifest.json")
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    fixture = {r["id"]: r for r in load(EXP / "requests.json")}
    source = PREVIOUS / "fixtures/requests.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest["source_requests_sha256"]
    assert hashlib.sha256((PREVIOUS / "fixtures/manifest.json").read_bytes()).hexdigest() == manifest["source_manifest_sha256"]
    assert list(fixture.values()) == [r for r in load(source) if r["description"] == "concise-en"]
    status = load(EXP / "results/suite-status.json")
    assert status["status"] == "partial", status
    assert [c["id"] for c in status["configs"]] == [c["id"] for c in manifest["configs"]]
    assert [c["status"] for c in status["configs"]] == ["complete"] * 3 + ["failed"] * 4
    retry_manifest = load(QWEN / "manifest.json")
    for name, expected in retry_manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    assert retry_manifest["configs"] == manifest["configs"][3:]
    assert (QWEN / "requests.json").read_bytes() == (EXP / "requests.json").read_bytes()
    retry_status = load(QWEN / "results/suite-status.json")
    assert retry_status["status"] == "complete" and len(retry_status["configs"]) == 4
    assert all(c["status"] == "complete" for c in retry_status["configs"])
    entries = {c["id"]: c for c in status["configs"][:3] + retry_status["configs"]}
    for config in manifest["configs"][3:]:
        assert not (EXP / "results" / config["id"] / "requests.jsonl").exists()
        assert (EXP / "results" / config["id"] / "error.json").exists()
    gpu_samples = {}
    for experiment in [EXP, QWEN]:
        with (experiment / "results/gpu.csv").open() as gpu_file:
            gpu_samples[experiment] = [
                (dt.datetime.strptime(r[0].strip(), "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc),
                 float(r[2].strip().split()[0]) / 1024)
                for r in list(csv.reader(gpu_file))[1:] if len(r) == 5
            ]
    reports, errors, verification = [], [], []
    for config in manifest["configs"]:
        experiment = QWEN if config["model"] in ["9B", "4B"] else EXP
        directory = experiment / "results" / config["id"]
        samples = gpu_samples[experiment]
        command = load(directory / "server-command.json")
        assert command[command.index("--nproc_per_node") + 1] == "1"
        assert command[command.index("--max-length") + 1] == str(manifest["max_length"])
        assert command[command.index("--max-length") + 2:] == config["flags"]
        assert command[command.index("--checkpoint") + 1] == "/opt/karpo/models/karpo-" + config["model"].lower()
        assert ("--fp8" in command) == (config["precision"] == "fp8")
        health = load(directory / "health.json")
        assert health["ranks"] == 1 and health["max_length"] == manifest["max_length"]
        assert health["path"] == ("fp8-graphs" if config["precision"] == "fp8" else "graphs")
        rows = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
        expected = {"warmup-excluded": 8, "quality-existing": 80, "quality-boundary": 24,
                    **{f"concurrency-{n}": manifest["concurrency_requests"] for n in config["concurrency"]}}
        assert collections.Counter(r["stage"] for r in rows) == expected, config["id"]
        assert sorted(r["sequence"] for r in rows) == list(range(1, len(rows) + 1))
        assert load(directory / "complete.json")["observations"] == len(rows)
        summary = load(directory / "summary.json")
        for row in rows:
            item = fixture[row["request_id"]]
            for key in ["case_id", "group", "order", "description", "candidate_count", "expected"]:
                assert row[key] == item[key], (config["id"], key)
            assert row["candidate_count"] == len(item["request"]["questions"]["action"]["criteria"]) == 200
            assert row["request_sha256"] == item["request_sha256"] == digest(item["request"])
            if row["valid"]:
                assert row["correct"] == validate(row["response"], item)
                assert row["response"]["model"] == config["id"]
                assert 0 < row["response"]["usage"]["input_tokens"] <= manifest["max_length"]
            else:
                assert not row["correct"]
            if row["stage"].startswith("quality-") and not row["correct"]:
                errors.append({"config": config["id"], "state": item["request"]["state"], **row})
        for stage in expected:
            selected = [r for r in rows if r["stage"] == stage]
            assert summarize(selected, summary[stage]["elapsed_seconds"]) == summary[stage]
            if stage.startswith("quality-"):
                group = "synthetic" if stage == "quality-existing" else "boundary"
                assert len({r["request_id"] for r in selected}) == expected[stage]
                assert {r["request_id"] for r in selected} == {
                    r["id"] for r in fixture.values() if r["group"] == group}
        quality = [r for r in rows if r["stage"].startswith("quality-")]
        pooled = summarize(quality, sum(summary[s]["elapsed_seconds"] for s in
                                       ["quality-existing", "quality-boundary"]))
        by_case = collections.defaultdict(dict)
        for row in quality:
            by_case[row["case_id"]][row["order"]] = row
        assert len(by_case) == 52 and all(set(p) == {"original", "reversed"} for p in by_case.values())
        complete_pairs = [p for p in by_case.values() if all(r["valid"] for r in p.values())]
        order_changes = sum(choice(p["original"]) != choice(p["reversed"]) for p in complete_pairs)
        entry = entries[config["id"]]
        started = dt.datetime.fromisoformat(entry["started_utc"])
        ready = started + dt.timedelta(seconds=entry["startup_seconds"])
        ended = dt.datetime.fromisoformat(entry["ended_utc"])
        startup_samples = [v for t, v in samples if started <= t < ready]
        inference_samples = [v for t, v in samples if ready <= t <= ended]
        assert startup_samples and inference_samples
        reports.append({
            **config, "label": config["model"] + " " + config["precision"].upper(),
            "result_directory": directory.relative_to(ROOT).as_posix(),
            "summary": summary, "quality": pooled, "observations": len(rows),
            "order_changes": order_changes, "valid_pairs": len(complete_pairs),
            "health": health,
            "startup_seconds": entry["startup_seconds"],
            "sampled_startup_peak_gib": max(startup_samples),
            "sampled_inference_peak_gib": max(inference_samples),
            "_quality_rows": quality,
        })
        verification.append({"config": config["id"], "observations": len(rows),
                             "result_directory": directory.relative_to(ROOT).as_posix(),
                             "raw_sha256": hashlib.sha256((directory / "requests.jsonl").read_bytes()).hexdigest()})
    assert sum(r["observations"] for r in reports) == manifest["expected_calls"] == 1168
    baseline = {r["request_id"]: r for r in reports[0]["_quality_rows"]}
    assert reports[0]["id"] == "31b-fp8"
    for report in reports:
        quality = report["_quality_rows"]
        report["new_errors_vs_31b"] = sum(baseline[r["request_id"]]["correct"] and not r["correct"] for r in quality)
        report["fixed_errors_vs_31b"] = sum(not baseline[r["request_id"]]["correct"] and r["correct"] for r in quality)
        report["same_choices_vs_31b"] = sum(r["valid"] and baseline[r["request_id"]]["valid"]
                                          and choice(r) == choice(baseline[r["request_id"]]) for r in quality)
    for size in ["12B", "9B", "4B"]:
        fp8, bf16 = [next(r for r in reports if r["model"] == size and r["precision"] == p)
                     for p in ["fp8", "bf16"]]
        reference = {r["request_id"]: r for r in bf16["_quality_rows"]}
        fp8["same_choices_vs_bf16"] = sum(
            r["valid"] and reference[r["request_id"]]["valid"]
            and choice(r) == choice(reference[r["request_id"]]) for r in fp8["_quality_rows"])
        fp8["new_errors_vs_bf16"] = sum(reference[r["request_id"]]["correct"] and not r["correct"]
                                       for r in fp8["_quality_rows"])
    for report in reports:
        del report["_quality_rows"]
    record = {"checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "all_complete": True, "total_calls": 1168, "checks": verification,
              "initial_startup_failures": 4, "initial_failed_config_decision_calls": 0,
              "retry_calls": 640}
    environment = load(EXP / "results/environment.json")
    capacity = load(ROOT / "experiments/h100-4b-20261120/results/capacity-status.json")
    assert capacity["model_calls"] == capacity["new_instances_created"] == 0
    assert len(capacity["attempts"]) == 20
    assert all(a["error_code"] == "InsufficientInstanceCapacity" for a in capacity["attempts"])
    assert environment["installed_source"]["vcs_info"]["commit_id"] == manifest["code_revision"]
    postflight = load(EXP / "results/postflight.json")
    assert postflight["suite_status"] == "partial" and not postflight["gpu_compute_processes"]
    retry_environment = load(QWEN / "results/environment.json")
    for key in ["torch", "transformers", "cuda", "code_revision", "runtime_source_sha256", "gpu"]:
        assert retry_environment[key] == environment[key], key
    assert retry_environment["qwen_dependencies"] == retry_manifest["dependencies"]
    retry_postflight = load(QWEN / "results/postflight.json")
    assert retry_postflight["suite_status"] == "complete" and not retry_postflight["gpu_compute_processes"]
    for model in manifest["models"]:
        download = load(EXP / "results" / ("download-" + model["size"] + ".json"))
        saved = next(m for m in postflight["models"] if m["size"] == model["size"])
        assert download["status"] == "complete" and download["model"] == model
        assert saved["weights_bytes"] == download["weights_bytes"] == model["weights_bytes"]
        assert saved["directory"] == download["directory"] == "/opt/karpo/models/karpo-" + model["size"].lower()
        if model["size"] in ["9B", "4B"]:
            retry_saved = next(m for m in retry_postflight["models"] if m["size"] == model["size"])
            assert retry_saved == saved
    (EXP / "results/verification.json").write_text(json.dumps(record, indent=2) + "\n")
    (EXP / "results/quality-errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n")
    return manifest, reports, errors, record


def main():
    manifest, reports, errors, verification = verify()
    replicas = verify_replicas(reports)
    candidates, candidate_examples = load_candidate_examples()
    (EXP / "results/candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n")
    decision_example = load_decision_example()
    (EXP / "results/decision-example.json").write_text(
        json.dumps(decision_example, ensure_ascii=False, indent=2) + "\n")
    cloud = documentation_metadata(load(EXP / "results/infrastructure.json"))
    assert cloud["state"] == "stopped", "Publish the final resource state after stopping the GPU"
    assert all(i["state"] == "stopped" for i in cloud["related_instances"])
    environment = load(EXP / "results/environment.json")
    eligible = [r for r in reports if r["new_errors_vs_31b"] == 0 and r["quality"]["valid"] == 104]
    preferred = min(eligible, key=lambda r: r["quality"]["p50_ms"])
    fastest = min(reports, key=lambda r: r["quality"]["p50_ms"])
    conclusion = (
        f"完整保留200候选、一次模型选择。最快配置是 {fastest['label']}："
        f"104次质量观察的P50 {fmt(fastest['quality']['p50_ms'])}ms、"
        f"P95 {fmt(fastest['quality']['p95_ms'])}ms，参考一致 {fastest['quality']['correct']}/104。"
        f"本批没有新增31B基线错误的最快配置为 {preferred['label']}，"
        f"P50 {fmt(preferred['quality']['p50_ms'])}ms，参考一致 {preferred['quality']['correct']}/104。"
        "七个配置均未达到100ms目标。"
    )
    quality_rows = [[r["label"], f'{r["summary"]["quality-existing"]["correct"]}/80',
                     f'{r["summary"]["quality-boundary"]["correct"]}/24', f'{r["quality"]["valid"]}/104',
                     fmt(r["quality"]["p50_ms"]), fmt(r["quality"]["p95_ms"]),
                     f'{r["quality"]["input_tokens_min"]}–{r["quality"]["input_tokens_max"]}'] for r in reports]
    stability_rows = [[r["label"], f'{r["order_changes"]}/{r["valid_pairs"]}',
                       r["new_errors_vs_31b"], r["fixed_errors_vs_31b"],
                       f'{r["same_choices_vs_31b"]}/104'] for r in reports]
    quantization_rows = [[r["model"], f'{r["same_choices_vs_bf16"]}/104', r["new_errors_vs_bf16"]]
                         for r in reports if "same_choices_vs_bf16" in r]
    memory_rows = [[r["label"], fmt(r["sampled_startup_peak_gib"], 2),
                   fmt(r["sampled_inference_peak_gib"], 2), fmt(r["startup_seconds"], 1)] for r in reports]
    concurrency_rows = [[r["label"], n, f'{s["valid"]}/{s["attempts"]}', f'{s["correct"]}/{s["attempts"]}',
                         fmt(s["valid_rps"], 3),
                         fmt(s["p50_ms"]), fmt(s["p95_ms"])]
                        for r in reports for n in r["concurrency"] for s in [r["summary"][f"concurrency-{n}"]]]
    replica_rows = [
        [r["replicas"], n, f'{s["valid"]}/{s["attempts"]}', f'{s["correct"]}/{s["attempts"]}',
         fmt(s["valid_rps"], 3),
         fmt(s["p50_ms"]), fmt(s["p95_ms"])]
        for r in replicas["configs"] for n in [1, 2, 4, 8, 16]
        for s in [r["summary"][f"concurrency-{n}"]]]
    one, two = replicas["configs"]
    rps_one = one["summary"]["concurrency-8"]["valid_rps"]
    rps_two = two["summary"]["concurrency-8"]["valid_rps"]
    replica_text = (
        f"官方副本对照使用 {replicas['selected']}，按预先冻结规则从本批FP8配置中选择。"
        f"并发8时，一张GPU吞吐 {fmt(rps_one, 3)} 请求/秒，两张GPU {fmt(rps_two, 3)} 请求/秒，"
        f"为单卡的 {fmt(rps_two/rps_one, 2)} 倍。"
        f"各104次质量观察的参考一致数为 {one['quality']['correct']}/104、{two['quality']['correct']}/104；"
        f"两次选择相同 {replicas['comparison']['same_choices']}/104。"
        "每张GPU各运行一份完整模型，增加副本主要影响吞吐和排队。"
        "同批数据选择及重复观察不能作为独立质量验证。"
    )
    historical_labels = {"original": "原始长描述", "concise-en": "英文精简",
                         "concise-zh": "中文精简", "factored": "公共规则 + 候选引用"}
    historical_rows = []
    for variant, label in historical_labels.items():
        old = load(PREVIOUS / "results" / variant / "summary.json")
        historical_rows.append([label, fmt(old["quality-existing"]["p50_ms"]),
                                f'{old["quality-existing"]["correct"]}/80',
                                f'{old["quality-boundary"]["correct"]}/24'])
    tables = [
        ("质量与延迟", ["配置", "原有正确", "边界正确", "有效", "P50 ms", "P95 ms", "输入tokens"], quality_rows),
        ("顺序稳定性与基线对照", ["配置", "换序变化/有效对", "比31B新增错误", "修正31B错误", "与31B选择相同"], stability_rows),
        ("FP8 与同模型 BF16 对照", ["模型", "选择相同", "FP8新增错误"], quantization_rows),
        ("单 GPU 并发扫描", ["配置", "并发", "有效/尝试", "参考一致", "有效请求/秒", "P50 ms", "P95 ms"], concurrency_rows),
        ("上次31B描述实验 · 2026-11-19", ["描述", "原有场景P50 ms", "原有正确", "边界正确"], historical_rows),
        ("同模型单副本与双副本", ["GPU副本", "并发", "有效/尝试", "参考一致", "有效请求/秒", "P50 ms", "P95 ms"], replica_rows),
        ("显存与服务准备", ["配置", "启动显存抽样峰值 GiB", "推理显存抽样峰值 GiB", "启动秒"], memory_rows),
    ]
    method = (
        "52个合成案例（40个原有案例、12个边界案例），每例两种候选顺序。每个配置104次质量观察，"
        "状态、参考答案、全部200候选及其英文精简描述沿用前次冻结输入；状态保留原有中英文。"
        "不进行程序业务路由或候选筛选。"
        "配对观察和重复请求不是新增独立案例，这些结果不能证明Redotpay生产准确率或SOTA。"
        "推荐条件是本批不新增31B已答对用例的错误，并不表示所有案例均正确。"
    )
    timing = (
        "系列模型对比每次使用一张GPU，所有配置依次运行并重测31B基线。HTTP客户端与模型同机，"
        "计时到完整响应，包含预处理和排队；不包含生产网络或工具执行。"
        "新表的P50/P95合并原有和边界共104次观察；历史描述表仅使用80次原有观察。"
        "每个配置8次预热单列，四个FP8配置各做并发1/2/4/8、每格24次的短时扫描。"
        "不同模型使用各自tokenizer；最大长度8192，不截断输入。"
    )
    memory_note = "显存每2秒抽样，可能遗漏瞬时峰值，不等于最小可部署容量；FP8启动仍需加载原始权重。"
    hardware = (
        f"实际实例 {cloud['instance_type']}，48vCPU、512GiB内存、两张RTX PRO 6000 Blackwell 96GB；"
        "系列模型表每次使用一张GPU。"
        "本轮在该实例上重新测试31B基线。"
        "B200及H100的容量尝试未成功，没有这些GPU的新性能数据。"
        "本次新增H100尝试覆盖us-east-1和us-west-2的10个可用区，Spot与按需共20次，均未取得容量。"
    )
    runtime = (
        f"部署使用Karpo官方HTTP服务，PyTorch {environment['torch']}、"
        f"Transformers {environment['transformers']}、CUDA {environment['cuda']}。"
        "实际启动参数和health响应保存在各配置结果目录。"
        "首轮9B/4B因缺少fla依赖而启动失败，决策请求为0；"
        "补装flash-linear-attention和fla-core 0.5.2后，在同一EC2实例重启后完成四个配置的独立重试。"
        "Karpo 31B/12B使用重启前已完成的数据，Karpo 9B/4B使用重启后的数据；模型和核心运行库版本不变。"
    )
    lines = ["# Karpo 系列模型：完整200候选实测", "", conclusion, "", replica_text, "", method, "",
             timing + memory_note, "", hardware, "", runtime, "",
             "## 完整决策示例", "",
             decision_example_markdown(decision_example, "results/decision-example.json"), "",
             "## 候选示例", "", candidate_examples_markdown(candidate_examples, "results/candidates.json"), ""]
    for title, headers, rows in tables:
        lines += ["## " + title, "", "| " + " | ".join(headers) + " |",
                  "|" + "|".join("---" for _ in headers) + "|"]
        lines += ["| " + " | ".join(map(str, row)) + " |" for row in rows]
        lines.append("")
    lines += ["## 运行与证据", "",
              f"系列模型核验1168次调用，副本对照544次，合计1712次；系列质量错误或无效观察 {len(errors)} 次。",
              f"实例 `{cloud['instance_id']}`，类型 `{cloud['instance_type']}`，状态 **{cloud['state']}**；"
              f"记录时间 {cloud['observed_utc']}。", "",
              "GPU：" + environment["gpu"].replace("\n", "；"), "",
              "只stop，不terminate，实例及EBS保留。", "",
              "[逐条错误](results/quality-errors.json) · [验证记录](results/verification.json) · "
              "[实验计划](PLAN.zh.md) · [冻结配置](manifest.json)", ""]
    markdown = "\n".join(lines)
    (EXP / "RESULTS.zh.md").write_text(markdown)
    prefix = EXP.relative_to(ROOT).as_posix()
    main_markdown = markdown.replace("](results/", f"]({prefix}/results/").replace(
        "](PLAN.zh.md)", f"]({prefix}/PLAN.zh.md)").replace("](manifest.json)", f"]({prefix}/manifest.json)")
    (ROOT / "RESULTS.zh.md").write_text(main_markdown)
    (EXP / "STATUS.zh.md").write_text(
        "# 实验已完成\n\n" + conclusion + "\n\n" + replica_text + "\n\n"
        "系列模型7个配置和副本对照2个配置均已完成，GPU实例已stop，实例与EBS保留。\n\n"
        "[完整结果](RESULTS.zh.md) · [HTML](report.html) · [资源记录](results/infrastructure.json)\n\n"
        "results/capacity-status.json保留启动成功之前的历史容量核对，不代表当前实验状态。\n")
    esc = html.escape
    example_table = table(
        CANDIDATE_EXAMPLE_HEADERS,
        [[e["id"], e["description"], e["meaning_zh"]] for e in candidate_examples],
        "实际测试候选示例",
    )
    candidate_section = (
        '<section id="candidate-examples"><div class="section-head"><h2>候选示例</h2></div>'
        f'<div class="panel"><div class="panel-padding text-block">{esc(CANDIDATE_EXAMPLE_NOTE)}</div>'
        + example_table
        + '<div class="table-caption"><a href="@@CANDIDATES@@">查看完整200候选 JSON</a></div></div></section>'
    )
    sections = "".join(
        f'<section id="part-{i}"><div class="section-head"><h2>{esc(title)}</h2></div>'
        f'<div class="panel">{table(headers, rows, title)}</div></section>'
        for i, (title, headers, rows) in enumerate(tables) if i in {0, 1, 3, 4})
    cards = "".join(
        f'<div class="kpi{" accent" if r["id"] == preferred["id"] else ""}">'
        f'<div class="kpi-top">{r["label"]} · P50</div>'
        f'<div class="kpi-value">{fmt(r["quality"]["p50_ms"])}<small>ms</small></div>'
        f'<div class="kpi-note">参考一致 {r["quality"]["correct"]}/104</div></div>'
        for r in reports if r["precision"] == "fp8")
    hidden_fields = {"startup_seconds", "sampled_startup_peak_gib", "sampled_inference_peak_gib",
                     "same_choices_vs_bf16", "new_errors_vs_bf16"}
    html_reports = [{key: value for key, value in r.items() if key not in hidden_fields} for r in reports]
    payload = json.dumps(documentation_metadata({
        "configs": html_reports, "manifest": documentation_metadata(manifest),
        "retry_manifest": documentation_metadata(load(QWEN / "manifest.json")), "verification": verification,
        "infrastructure": cloud, "preferred": preferred["id"], "quality_error_count": len(errors),
        "candidate_examples": candidate_examples,
        "decision_example": decision_example,
    }), ensure_ascii=False).replace("<", "\\u003c")
    css = (ROOT / "scripts/report.css").read_text()
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Karpo 31B / 12B / 9B / 4B 实测</title>
<style>{css}
#candidate-examples table{{table-layout:fixed}}
#candidate-examples th,#candidate-examples td{{white-space:normal;overflow-wrap:anywhere;vertical-align:top}}
#candidate-examples th:first-child{{width:100px}}
#candidate-examples th:nth-child(2){{width:55%}}
.example-code{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.65;background:#f4f7f8;padding:16px;border-radius:8px}}
#decision-example h3{{margin:24px 0 12px}}
#decision-example blockquote{{margin:16px 0;padding:12px 16px;border-left:3px solid #168178;background:#f4f7f8}}
#decision-example details{{margin:16px 0}}
#decision-example summary{{cursor:pointer;font-weight:600}}
.example-options table{{table-layout:fixed}}
.example-options th,.example-options td{{white-space:normal;overflow-wrap:anywhere;vertical-align:top}}
.example-options th:first-child{{width:100px}}
.candidate-catalog{{max-height:460px;overflow:auto;margin-top:16px}}
@media print{{.candidate-catalog{{max-height:none;overflow:visible}}}}
@media(max-width:560px){{#candidate-examples th:first-child{{width:76px}}#candidate-examples th:nth-child(2){{width:48%}}}}
</style></head><body><aside class="sidebar">
<a class="brand" href="#overview"><span class="brand-mark">K</span> Karpo Decision</a>
<div class="side-label">FULL 200 / MODEL FAMILY</div><nav class="nav"><a href="#overview">结论与延迟</a>
<a href="#decision-example">完整决策示例</a>
<a href="#candidate-examples">候选示例</a>
<a href="#part-0">选择质量</a><a href="#part-1">顺序与基线</a>
<a href="#part-3">单卡并发</a><a href="#part-4">历史描述实验</a>
<a href="#methods">口径与资源</a></nav></aside>
<main><div class="topbar"><div class="crumb">2026-11-20 / 同规格单GPU模型对比</div>
<div class="top-actions"><button class="button" id="download">下载汇总数据</button>
<button class="button primary" onclick="window.print()">打印 / PDF</button></div></div>
<section id="overview"><div class="eyebrow">200 CANDIDATES · FOUR MODELS · FP8 / BF16</div>
<h1 style="font-size:clamp(26px,3vw,36px);line-height:1.4;margin:16px 0">完整200候选，<br>单卡P50最低 {fmt(fastest['quality']['p50_ms'])} ms</h1>
<p class="text-block">{esc(conclusion)}</p><div class="pills"><span class="pill green">7/7配置完成</span>
<span class="pill">1168次系列调用</span><span class="pill">同机重测31B基线</span>
<span class="pill">52个合成案例 · 两种候选顺序</span>
<span class="pill">系列表为单卡结果</span></div><div class="kpis">{cards}</div>
<div class="panel panel-padding"><div class="panel-header"><div><h3>104次质量观察的完整响应延迟</h3>
<p class="panel-sub">零起点线性刻度 · 100ms目标线 · FP8与BF16均展示</p></div>
<div class="segmented controls"><button data-metric="p50_ms" aria-pressed="true">P50</button>
<button data-metric="p95_ms" aria-pressed="false">P95</button></div></div><div id="bars" class="bar-chart"></div></div>
</section>{decision_example_html(decision_example)}
{candidate_section}{sections}<section id="methods"><div class="section-head"><h2>测试口径与资源</h2></div>
<div class="panel panel-padding text-block"><p>{esc(method)}</p><p>{esc(timing)}</p><p>{esc(hardware)}</p>
<p>{esc(runtime)}</p>
<p>质量错误或无效观察共 {len(errors)} 条；<a href="@@ERRORS@@">查看逐条错误和原始响应</a>。</p>
<p>本次实例 {esc(cloud['instance_id'])}，状态 {esc(cloud['state'])}。只stop，不terminate，模型保存在EBS。</p>
<details><summary>资源记录</summary><pre class="code-line" style="white-space:pre-wrap">{esc(json.dumps(cloud,ensure_ascii=False,indent=2))}</pre></details>
</div></section><footer class="footer"><span>离线报告 · 所有指标由逐条响应重算</span><div class="footer-links">
<a href="@@RESULTS@@">Markdown</a><a href="@@PLAN@@">实验计划</a><a href="@@README@@">复现说明</a>
<a href="@@RETRY@@">Karpo 9B/4B重试记录</a>
<a href="@@HARDWARE@@">H100容量记录</a>
</div></footer></main><script type="application/json" id="data">{payload}</script>"""
    page += r"""<script>
const data=JSON.parse(document.getElementById('data').textContent);
const fmt=n=>n.toLocaleString('zh-CN',{minimumFractionDigits:1,maximumFractionDigits:1});
function render(metric){
const max=Math.ceil(Math.max(100,...data.configs.map(c=>c.quality[metric]))/100)*100;
document.getElementById('bars').innerHTML=data.configs.map(c=>`<div class="bar-row">
<div class="bar-label"><b>${c.label}</b></div><div class="bar-track"><div class="bar-fill"
style="width:${c.quality[metric]/max*100}%;background:${c.precision==='fp8'?'#168178':'#a9b5c1'}"></div>
<span class="bar-goal" style="left:${100/max*100}%" aria-hidden="true"></span></div>
<div class="bar-value">${fmt(c.quality[metric])} <small>ms</small></div></div>`).join('');
}
document.querySelectorAll('[data-metric]').forEach(b=>b.addEventListener('click',()=>{
document.querySelectorAll('[data-metric]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));render(b.dataset.metric)}));
document.getElementById('download').addEventListener('click',()=>{
const u=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
const a=document.createElement('a');a.href=u;a.download='karpo-family-results.json';a.click();
setTimeout(()=>URL.revokeObjectURL(u),1000);});
let closed=[];window.addEventListener('beforeprint',()=>{closed=[...document.querySelectorAll('details:not([open])')];closed.forEach(x=>x.open=true)});
window.addEventListener('afterprint',()=>closed.forEach(x=>x.open=false));render('p50_ms');
</script></body></html>"""
    targets = {"ERRORS": EXP / "results/quality-errors.json", "RESULTS": EXP / "RESULTS.zh.md",
               "PLAN": EXP / "PLAN.zh.md", "README": ROOT / "README.md",
               "RETRY": QWEN / "PLAN.zh.md",
               "CANDIDATES": EXP / "results/candidates.json",
               "DECISION": EXP / "results/decision-example.json",
               "HARDWARE": ROOT / "experiments/h100-4b-20261120/STATUS.zh.md"}
    for destination in [ROOT / "report.html", EXP / "report.html"]:
        destination.write_text(re.sub(r"@@([A-Z]+)@@",
                                     lambda m: esc(os.path.relpath(targets[m[1]], destination.parent)), page))
    print(conclusion)


if __name__ == "__main__":
    main()
