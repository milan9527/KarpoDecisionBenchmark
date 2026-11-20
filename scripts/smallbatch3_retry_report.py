"""Validate every completed call and publish the original-input replica comparison."""
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re

from batch100_benchmark import percentile, summary, validate, write

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/smallbatch3-retry-20261120"
RESULT = EXP / "results"
LABELS = {"risk": "风控", "sop": "SOP"}
SCOPE = ("从主机本地客户端发起请求，到收到分发器完整HTTP响应；1/2副本在本机，"
         "3副本跨两台机器，包含私网转发。全部组合经过相同分发器；不含生产入口及特征查询。")


def load(path):
    return json.loads(path.read_text())


def fmt(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def verify():
    manifest = load(EXP / "manifest.json")
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    fixtures = {item["id"]: item for scenario in LABELS for item in load(EXP / f"{scenario}-requests.json")}
    references = {scenario: {r["request_id"]: r for r in map(json.loads, (
        ROOT / f"experiments/batch100-20261120/results/fp8/{scenario}/quality-serial/requests.jsonl"
    ).read_text().splitlines())} for scenario in LABELS}
    stages, errors = {}, []
    for path in sorted((RESULT / "measurements").glob("**/requests.jsonl")):
        if not (path.parent / "complete.json").exists():
            continue
        key = str(path.parent.relative_to(RESULT / "measurements"))
        scenario = key.split("/")[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        stored = load(path.parent / "summary.json")
        recomputed = summary(rows, stored["elapsed_seconds"])
        for name, value in recomputed.items():
            assert stored[name] == value, (key, name)
        params = stored["parameters"]
        assert len({row["sequence"] for row in rows}) == len(rows), key
        if params.get("count") is not None:
            assert len(rows) == params["count"], key
        if params.get("seconds"):
            assert stored["elapsed_seconds"] >= 60
            middle = [r for r in rows if 10 <= r["start_offset_s"] < 60]
            assert summary(middle, stored["after_ramp"]["elapsed_seconds"]) == stored["after_ramp"]
        parity_details = []
        for row in rows:
            item = fixtures[row["request_id"]]
            assert row["request_sha256"] == item["request_sha256"], key
            if row["valid"]:
                assert row["http_status"] == 200 and validate(row["response"], item) == row["correct"]
                assert row["response"]["metrics"]["packs"] == 1, key
                assert row["response"]["metrics"]["graph_replays"] == 1, key
            if path.parent.name == "quality":
                if not row["valid"]:
                    parity_details.append({"request_id": item["id"], "invalid": True})
                else:
                    ref = references[scenario][item["id"]]
                    actual = row["response"]["answers"]["action"]
                    before = ref["response"]["answers"]["action"]
                    parity_details.append({
                        "request_id": item["id"], "same_choice": actual["choice"] == before["choice"],
                        "reference_correct": ref["correct"], "actual_correct": row["correct"],
                        "max_probability_delta": max(abs(p - actual["probabilities"][k])
                                                     for k, p in before["probabilities"].items()),
                    })
                if not row["correct"]:
                    errors.append({"stage": key, "request_id": item["id"], "expected": item["expected"],
                                   "valid": row["valid"], "actual": row.get("response", {}).get("answers")})
        if path.parent.name == "quality":
            expected_quality = {k for k, v in fixtures.items() if v["scenario"] == scenario and v["group"] == "quality"}
            assert {r["request_id"] for r in rows} == expected_quality
            parity = {
                "observations": len(rows), "same_choices": sum(r.get("same_choice", False) for r in parity_details),
                "new_errors": sum(r.get("reference_correct", False) and not r.get("actual_correct", False)
                                  for r in parity_details),
                "max_probability_delta": max((r.get("max_probability_delta", 0) for r in parity_details), default=None),
                "details": parity_details,
            }
            assert parity == load(path.parent / "parity-vs-reference.json")
        else:
            parity = None
        metrics = [r["response"]["metrics"] for r in rows if r["valid"]]
        devices = Counter(str(m["global_gpu"]) for m in metrics)
        match = re.search(r"/r(\d+)-b(\d+)-w0/", "/" + key)
        replicas = int(match[1]) if match else 3
        if match:
            assert all(m["batch_size"] <= int(match[2]) for m in metrics), key
        assert set(devices) <= {str(i) for i in range(replicas)}, key
        intervals = [(r["start_offset_s"] + r["send_lag_ms"] / 1000,
                      r["start_offset_s"] + r["latency_ms"] / 1000) for r in rows]
        inflight = peak = 0
        for _, change in sorted(e for start, end in intervals for e in ((start, 1), (end, -1))):
            inflight += change
            peak = max(peak, inflight)
        assert inflight == 0
        mean_inflight = None
        if params.get("seconds"):
            assert params == {"concurrency": 100, "seconds": 60}, key
            assert peak == 100 and set(devices) == {str(i) for i in range(replicas)}, key
            mean_inflight = sum(max(0, min(end, 60) - max(start, 10)) for start, end in intervals) / 50
            assert mean_inflight > 99, key
        stages[key] = {
            **stored, "latency_scope": SCOPE, "gpu_response_counts": dict(devices),
            "peak_client_inflight": peak, "mean_client_inflight_seconds_10_to_60": mean_inflight,
            "timing_p50_ms": {k: percentile([m[k] for m in metrics], .5)
                              for k in ("queue_ms", "encode_ms", "model_ms")},
            "parity": parity,
        }
    complete = (RESULT / "measurements/complete.json").exists()
    if complete:
        expected = {f"{scenario}/r{replicas}-b{batch}-w0/{phase}"
                    for scenario in LABELS for replicas in (1, 2, 3) for batch in (1, 2, 4)
                    for phase in ("warmup", "quality", "sustained-100")}
        expected |= {f"{scenario}/{phase}" for scenario in LABELS for phase in ("confirm-warmup", "confirm-100")}
        assert set(stages) == expected, (expected - set(stages), set(stages) - expected)
    for role in ("primary", "secondary"):
        postflight = RESULT / role / "postflight.json"
        if postflight.exists():
            post = load(postflight)
            assert not post["gpu_compute_processes"].strip()
            assert post["model_weights_bytes"] == 9081227392
    verification = {
        "status": "complete" if complete else "in_progress", "stages": len(stages),
        "http_attempts": sum(s["attempts"] for s in stages.values()),
        "valid_http_responses": sum(s["valid"] for s in stages.values()),
        "failures": sum(s["failures"] for s in stages.values()),
        "quality_error_observations": len(errors),
        "frozen_files_verified": len(manifest["files"]),
    }
    write(RESULT / "verification.json", verification)
    write(RESULT / "quality-errors.json", errors)
    return stages, verification


def main():
    stages, verification = verify()
    matrix, confirmations = [], []
    for scenario in LABELS:
        for replicas in (1, 2, 3):
            for batch in (1, 2, 4):
                key = f"{scenario}/r{replicas}-b{batch}-w0"
                if key + "/sustained-100" not in stages:
                    continue
                performance, quality = stages[key + "/sustained-100"], stages[key + "/quality"]
                matrix.append({
                    "scenario": scenario, "replicas": replicas, "batch": batch,
                    "performance": performance, "quality": quality,
                    "passes": performance["failures"] == quality["failures"] == 0 and
                              performance["p50_ms"] <= 500 and quality["accuracy_all_attempts"] > .90,
                })
        if f"{scenario}/confirm-100" in stages:
            candidates = [r for r in matrix if r["scenario"] == scenario and r["replicas"] == 3
                          and r["performance"]["failures"] == r["quality"]["failures"] == 0
                          and r["quality"]["accuracy_all_attempts"] > .90]
            chosen = min(candidates, key=lambda r: (r["performance"]["p50_ms"], -r["performance"]["valid_rps"]))
            selected = load(RESULT / "measurements" / scenario / "selection.json")["selected"]
            assert (selected["replicas"], selected["batch"]) == (3, chosen["batch"])
            confirmations.append({"scenario": scenario, "batch": chosen["batch"], "quality": chosen["quality"],
                                  "performance": stages[f"{scenario}/confirm-100"]})
    resource_path = RESULT / "infrastructure-final.json"
    resource = load(resource_path) if resource_path.exists() else None
    environments = {role: load(RESULT / role / "environment.json") for role in ("primary", "secondary")
                    if (RESULT / role / "environment.json").exists()}
    if len(environments) == 2:
        for key in ("torch", "transformers", "cuda", "dependencies", "runtime_source_sha256", "model_metadata_sha256"):
            assert environments["primary"][key] == environments["secondary"][key], key
    startup_path = RESULT / "startup-disk-prewarm.json"
    startup = load(startup_path) if startup_path.exists() else None
    primary_path = RESULT / "primary/suite-status.json"
    if startup and primary_path.exists():
        primary_status = load(primary_path)
        if primary_status.get("benchmark_started_utc"):
            assert startup["result"]["Status"] == "Success"
            from datetime import datetime
            assert datetime.fromisoformat(startup["result"]["ExecutionEndDateTime"].replace("Z", "+00:00")) <= (
                datetime.fromisoformat(primary_status["benchmark_started_utc"]))
    best_by_replicas = []
    for scenario in LABELS:
        for replicas in (1, 2, 3):
            candidates = [r for r in matrix if r["scenario"] == scenario and r["replicas"] == replicas]
            if len(candidates) != 3:
                continue
            eligible = [r for r in candidates if r["performance"]["failures"] == r["quality"]["failures"] == 0
                        and r["quality"]["accuracy_all_attempts"] > .9]
            if eligible:
                best_by_replicas.append(min(eligible, key=lambda r: (
                    r["performance"]["p50_ms"], -r["performance"]["valid_rps"])))
    data = {"verification": verification, "acceptance": {"concurrency": 100, "p50_ms": 500, "quality_min_exclusive": .9},
            "matrix": matrix, "confirmations": confirmations, "stages": stages, "resource": resource,
            "environment": environments, "latency_scope": SCOPE, "startup_disk_prewarm": startup,
            "best_by_replicas": best_by_replicas}
    write(EXP / "data.json", data)
    lines = [
        "# Karpo 4B FP8：保持原始输入的小batch与GPU副本验证", "",
        "风控200特征、SOP完整200候选，输入文件与上一轮逐字一致。每张RTX PRO 6000 Blackwell 96GB加载一个完整模型副本；"
        "1/2副本在原两卡机，3副本由g7e.12xlarge两卡加g7e.4xlarge单卡组成，CPU和内存配置不同。FP8、模型及运行库保持一致。", "",
        "**验收：持续100个在途请求，完整响应P50≤500ms；失败单列，合成质量>90%。** 每组合持续投递60秒后排空，等待时间0ms。", "",
        SCOPE, "",
        "## 三副本独立重复确认", "",
        "| 场景 | batch | P50 ms | P95 ms | 有效请求/秒 | 有效/尝试 | 合成质量 | 延迟达标 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in confirmations:
        p, q = row["performance"], row["quality"]
        passed = p["failures"] == 0 and p["p50_ms"] <= 500
        lines.append(f"| {LABELS[row['scenario']]} | {row['batch']} | {fmt(p['p50_ms'])} | {fmt(p['p95_ms'])} | "
                     f"{fmt(p['valid_rps'],2)} | {p['valid']}/{p['attempts']} | {q['correct']}/{q['attempts']} | "
                     f"{'是' if passed else '否'} |")
    lines.extend(["", "从三副本三个batch中按P50选出配置后，独立再做60秒确认；质量沿用该配置的完整质量集观察。"
                  "所有观察重复使用已有合成输入，不代表客户生产准确率。", "",
                  "## 副本扩展对照", "",
                  "每档副本数从batch 1/2/4完整矩阵中选择P50最低且质量合格的配置。下表为矩阵测量，"
                  "上表为三副本配置的独立重复确认。", "",
                  "| 场景 | GPU副本 | 最低P50的batch | P50 ms | 请求/秒 |",
                  "|---|---:|---:|---:|---:|"])
    for row in best_by_replicas:
        p = row["performance"]
        lines.append(f"| {LABELS[row['scenario']]} | {row['replicas']} | {row['batch']} | "
                     f"{fmt(p['p50_ms'])} | {fmt(p['valid_rps'],2)} |")
    lines.extend(["", "## 三副本确认阶段的耗时", "",
                  "| 场景 | 排队P50 ms | 编码P50 ms | 模型P50 ms |",
                  "|---|---:|---:|---:|"])
    for row in confirmations:
        timing = row["performance"]["timing_p50_ms"]
        lines.append(f"| {LABELS[row['scenario']]} | {fmt(timing['queue_ms'])} | "
                     f"{fmt(timing['encode_ms'])} | {fmt(timing['model_ms'])} |")
    lines.extend(["", "100并发下排队占了大部分时间。增加副本提高处理能力，排队时间随之减少；"
                  "本轮增大batch没有提高总吞吐。分项分位数不能相加，完整HTTP耗时还包括分发器及传输。"])
    lines.extend(["",
                  "## 全部持续100并发组合", "",
                  "| 场景 | 副本 | batch上限 | P50 ms | P95 ms | 请求/秒 | 失败 | ≤500ms/尝试 | 质量 | FP8新增错误 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for row in matrix:
        p, q = row["performance"], row["quality"]
        lines.append(f"| {LABELS[row['scenario']]} | {row['replicas']} | {row['batch']} | {fmt(p['p50_ms'])} | "
                     f"{fmt(p['p95_ms'])} | {fmt(p['valid_rps'],2)} | {p['failures']} | "
                     f"{p['valid_within_500ms']}/{p['attempts']} | {q['correct']}/{q['attempts']} | "
                     f"{q['parity']['new_errors']} |")
    lines.extend(["", "质量新增错误相对上一轮同输入FP8单请求参考；所有原序/反序观察保留。"
                  "batch上限不等于实际每批大小，实际分布见data.json。", "",
                  "## 核验与范围", "",
                  f"- 已核验{verification['stages']}个阶段、{verification['http_attempts']}次HTTP尝试；"
                  f"有效{verification['valid_http_responses']}次，失败{verification['failures']}次。",
                  "- 根据每条调用起止时间重建实际在途请求数，持续段要求峰值100、10–60秒平均大于99；核对使用的全部GPU编号。",
                  "- 每次输入都完整前向计算；没有规则分流、候选过滤、输入压缩、答案缓存或截断。",
                  "- 小batch图仅捕获本轮1–4大小；模型执行逻辑复用冻结原生服务。没有测试vLLM。",
                  "- 新增副机在前次启动中完成EBS模型文件预读，耗时约443秒；预读在正式测试前完成。加载、图编译及预热时间单独记录，不计入稳定服务的请求延迟。",
                  "- 原四副本计划因按需容量不足未执行；实际3副本已用满本区域Spot 64 vCPU配额，未获得四副本测量结果。容量尝试保留在provisioning.json。",
                  "- 前次三副本启动因缺少协调状态文件的S3读取权限退出，正式测量调用为0。本次补齐精确对象的读取权限，并在两台机器加载模型前实际验证；旧日志和源码保留。",
                  "- 原始summary.json由上一轮通用统计器生成，其latency_scope文案沿用同机描述；本报告及data.json按实际跨机器路径更正。",
                  "- 排队/编码/模型分项为按请求加权的批次耗时，分位数不能直接相加。",
                  "- 两台主机的运行库、模型元数据及推理源码哈希已比对；跨主机仍包含CPU、GPU及网络状态差异。", ""])
    if resource:
        states = "；".join(f"{i['instance_id']}：{i['state']}" for i in resource["instances"])
        lines.extend(["## 资源", "", states + "。仅stop，没有terminate；模型和EBS保留。",
                      f"核对时间：{resource['observed_utc']}。", ""])
    lines.extend(["[实验计划](PLAN.zh.md) · [全部数据](data.json) · [核验](results/verification.json) · "
                  "[质量错误](results/quality-errors.json) · [基础设施](results/provisioning.json)", ""])
    (EXP / "RESULTS.zh.md").write_text("\n".join(lines))
    scaling_rows = []
    for row in best_by_replicas:
        p = row["performance"]
        cells = "".join(f"<td>{html.escape(str(v))}</td>" for v in (
            LABELS[row["scenario"]], row["replicas"], row["batch"],
            fmt(p["p50_ms"]), fmt(p["valid_rps"], 2)))
        scaling_rows.append(f"<tr>{cells}</tr>")
    scaling = ('<h2>副本扩展对照</h2><p class="note">每档副本数从三个batch中选择P50最低且质量合格的配置。'
               '此表为矩阵测量，上方为三副本独立重复确认。</p><div class="scroll">'
               '<table id="scaling"><thead><tr><th>场景</th><th>GPU副本</th><th>batch</th>'
               '<th>P50 ms</th><th>请求/秒</th></tr></thead><tbody>' +
               "".join(scaling_rows) + '</tbody></table></div>')
    table_rows = []
    for row in matrix:
        p, q = row["performance"], row["quality"]
        values = [LABELS[row["scenario"]], row["replicas"], row["batch"], fmt(p["p50_ms"]), fmt(p["p95_ms"]),
                  fmt(p["valid_rps"], 2), p["failures"], f"{p['valid_within_500ms']}/{p['attempts']}",
                  f"{q['correct']}/{q['attempts']}", q["parity"]["new_errors"]]
        cells = "".join(f"<td>{html.escape(str(v))}</td>" for v in values)
        table_rows.append(f'<tr data-scenario="{row["scenario"]}" data-replicas="{row["replicas"]}" '
                          f'data-batch="{row["batch"]}">{cells}</tr>')
    cards = []
    for row in confirmations:
        p = row["performance"]
        cards.append(f'<article><h2>{LABELS[row["scenario"]]} · 三副本确认</h2>'
                     f'<strong>{fmt(p["p50_ms"])}<small> ms P50</small></strong>'
                     f'<p>batch {row["batch"]} · {fmt(p["valid_rps"], 2)} 请求/秒 · '
                     f'{p["failures"]} 次失败</p>'
                     f'<p class="note">排队P50 {fmt(p["timing_p50_ms"]["queue_ms"])} ms · '
                     f'模型P50 {fmt(p["timing_p50_ms"]["model_ms"])} ms</p>'
                     f'<p>{"达到" if p["failures"] == 0 and p["p50_ms"] <= 500 else "未达到"} 500ms 延迟要求</p></article>')
    resource_text = "资源状态待最终核对"
    if resource:
        resource_text = "所有相关GPU实例均已停止，模型与EBS保留。" if all(
            i["state"] == "stopped" for i in resource["instances"]) else "资源状态详见记录。"
        resource_text += " 核对时间：" + resource["observed_utc"]
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Karpo 小batch与GPU副本实测</title>
<style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#16253a;background:#f4f6fa}
body{max-width:1180px;margin:0 auto;padding:32px 24px 56px}h1{font-size:clamp(26px,4vw,40px);line-height:1.25}
p{line-height:1.8}a{color:#1851a3}header>p{max-width:950px}.eyebrow{color:#53657d;font-size:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px;margin:26px 0}
article{background:white;border:1px solid #dce3ef;border-radius:14px;padding:22px}article h2{font-size:18px}
strong{font-size:34px}strong small{font-size:16px;font-weight:400}.scroll{overflow:auto;border:1px solid #dce3ef;border-radius:10px}
table{border-collapse:collapse;background:white;width:100%;font-variant-numeric:tabular-nums;white-space:nowrap}
th,td{padding:13px 14px;text-align:right;border-bottom:1px solid #e6eaf1}th{background:#eaf0f8}th:first-child,td:first-child{text-align:left}
select{font:inherit;padding:8px 12px;border:1px solid #bbc8da;border-radius:6px;margin:0 12px 16px 0}
.note{color:#4a5d76;font-size:14px}footer{margin-top:28px;padding-top:18px;border-top:1px solid #dce3ef}
@media(max-width:600px){body{padding:20px 14px}.cards{gap:12px}article{padding:16px}}
</style><header><div class="eyebrow">Karpo 4B FP8 · 原始输入 · 2026-11-20</div>
<h1>小 batch 与 GPU 副本实测</h1>
<p>风控保留全部200特征，SOP保留完整200候选。对照1、2、3个GPU副本和batch上限1、2、4，持续维持100个在途请求，目标为完整响应P50≤500ms。</p>
<p class="note">SCOPE</p></header><section class="cards">CARDS</section>
SCALING
<h2>全部组合 · 持续100并发60秒</h2>
<label for="scenario">场景 </label><select id="scenario"><option value="">全部</option><option value="risk">风控</option><option value="sop">SOP</option></select>
<label for="replicas">GPU副本 </label><select id="replicas"><option value="">全部</option><option>1</option><option>2</option><option>3</option></select>
<div class="scroll"><table id="matrix"><thead><tr><th>场景</th><th>副本</th><th>batch上限</th><th>P50 ms</th><th>P95 ms</th><th>请求/秒</th><th>失败</th><th>≤500ms/尝试</th><th>合成质量</th><th>新增错误</th></tr></thead><tbody>ROWS</tbody></table></div>
<p class="note">每组合先预热和完整质量对照，再压测。三副本中P50最低且质量合格的配置独立重复确认，显示于上方。新增错误相对上一轮FP8参考；质量来自已有合成案例，不代表客户生产准确率。每批实际大小及原始响应均保留。</p>
<p class="note">全部组合经过同一本机分发器；三副本跨两台机器，增加私网转发成本。每卡一个完整模型，未测试vLLM。等待时间均为0ms。保持相同模型及推理逻辑，CUDA Graph只捕获本轮需要的batch大小。</p>
<p class="note">三副本由g7e.12xlarge两卡加g7e.4xlarge单卡组成，CPU和内存配置不同。原四副本计划因容量不足未执行。前次启动的权限问题及本次修复均保留记录；正式测试前已在两台机器验证权限。</p>
<footer><p>RESOURCE</p><p><a href="RESULTS.zh.md">完整文字报告</a> · <a href="data.json" download>下载数据</a> ·
<a href="PLAN.zh.md">实验计划</a> · <a href="results/verification.json">逐条核验</a> ·
<a href="results/quality-errors.json">质量错误</a></p></footer>
<script>
const scenario=document.getElementById('scenario'),replicas=document.getElementById('replicas');
function filter(){for(const row of document.querySelectorAll('#matrix tbody tr')){
row.hidden=!!((scenario.value&&row.dataset.scenario!==scenario.value)||(replicas.value&&row.dataset.replicas!==replicas.value));}}
scenario.addEventListener('change',filter);replicas.addEventListener('change',filter);
</script></html>"""
    document = document.replace("SCOPE", html.escape(SCOPE)).replace("CARDS", "".join(cards))
    document = document.replace("SCALING", scaling).replace("ROWS", "".join(table_rows)).replace(
        "RESOURCE", html.escape(resource_text))
    (EXP / "report.html").write_text(document)
    paths = [*EXP.rglob("*"), ROOT / "scripts/smallbatch3_retry_report.py", ROOT / "tests/test_smallbatch.py"]
    evidence = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(paths) if p.is_file() and p.name != "evidence-sha256.json"}
    write(RESULT / "evidence-sha256.json", evidence)
    print(json.dumps(verification))


if __name__ == "__main__":
    main()
