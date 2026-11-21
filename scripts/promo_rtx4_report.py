"""Recompute actual Karpo metrics from raw responses, not reference-policy output."""
import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import html
import json
from pathlib import Path

from benchmark import percentile, validate
from promo_risk_evaluate import evaluate_responses
from vllm_rps_benchmark import summarize

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-risk-rtx4-20261121"


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build(name):
    directory = EXP / "results/runs" / name
    status = read(directory / "status.json")
    if status["status"] != "complete":
        raise RuntimeError("A completed model run is required")
    manifest = read(directory / "manifest.json")
    for relative, expected in manifest["files"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Frozen runtime changed: {relative}")
    fixtures = read(EXP / "risk-requests.json")
    by_id = {r["id"]: r for r in fixtures}
    encodings = {r["request_id"]: r for r in read(EXP / "encoding-reference.json")}
    assert set(encodings) == set(by_id)
    stages, all_raw = [], {}
    for step in status["stages"]:
        stage = step["name"]
        rows = [json.loads(line) for line in (directory / stage / "requests.jsonl").read_text().splitlines()]
        assert sorted(r["sequence"] for r in rows) == list(range(len(rows)))
        for row in rows:
            fixture = by_id[row["request_id"]]
            assert row["request_sha256"] == fixture["request_sha256"]
            assert row["expected"] == fixture["expected"]
            assert abs(row["latency_ms"] - 1000*(row["end_offset_s"]-row["scheduled_offset_s"])) < 0.0001
            assert abs(row["http_latency_ms"] - 1000*(row["end_offset_s"]-row["send_offset_s"])) < 0.0001
            recomputed = False
            correct = False
            try:
                assert row.get("http_status") == 200
                correct = validate(row["response"], fixture)
                encoded = encodings[row["request_id"]]
                metrics = row["response"]["metrics"]
                assert metrics["tokens_sha256"] == encoded["tokens_sha256"]
                assert metrics["marker"] == encoded["marker"]
                assert row["response"]["usage"]["input_tokens"] == encoded["length"]
                assert metrics["replica_id"] == metrics["proxy_replica"]
                assert metrics["replica_id"] in range(4)
                assert metrics["backend"] == "vllm" and metrics["max_num_seqs"] == 4
                recomputed = True
            except (AssertionError, KeyError, TypeError, ValueError):
                pass
            assert row["valid"] == recomputed, row["request_id"]
            if recomputed:
                assert row["correct"] == correct
            else:
                row["correct"] = False
        all_raw[stage] = rows
        if stage in ("warmup", "quality"):
            expected_rows = [f for f in fixtures if f["group"] == ("quality" if stage == "quality" else "tune")]
            if stage == "warmup":
                expected_rows = expected_rows[:80]
            assert sorted(r["request_id"] for r in rows) == sorted(r["id"] for r in expected_rows)
            result = {
                "attempts": len(rows), "valid": sum(r["valid"] for r in rows),
                "correct": sum(r["valid"] and r["correct"] for r in rows),
                "failures": sum(not r["valid"] for r in rows),
                "p50_ms": percentile([r["latency_ms"] for r in rows if r["valid"]], .5),
                "p95_ms": percentile([r["latency_ms"] for r in rows if r["valid"]], .95),
            }
            result["accuracy"] = result["correct"]/result["attempts"]
        else:
            old = read(directory / stage / "summary.json")
            rate, seconds = (40,20) if stage == "low-load" else (100,20 if stage == "probe" else 120)
            tune = [f for f in fixtures if f["group"] == "tune"]
            for row in rows:
                assert row["request_id"] == tune[row["sequence"] % len(tune)]["id"]
            result = summarize(rows, rate, seconds, old["elapsed_with_drain_s"])
            for key in ("attempts", "valid", "correct", "failures", "passes_performance_acceptance",
                        "p50_ms", "p95_ms", "steady_valid_rps"):
                assert result[key] == old[key], (stage,key,result[key],old[key])
        stages.append({"name": stage, **result})
        stages[-1]["http_status_counts"] = dict(sorted(
            Counter(str(r.get("http_status")) for r in rows).items()))
        if stage in ("confirm", "repeat"):
            stages[-1]["per_replica"] = []
            for replica in range(4):
                selected = [r for r in rows if r["valid"] and
                            r["response"]["metrics"]["replica_id"] == replica]
                assert selected, f"No valid requests on replica {replica}"
                stages[-1]["per_replica"].append({
                    "replica": replica, "valid": len(selected),
                    "correct": sum(r["correct"] for r in selected),
                    "p50_ms": percentile([r["latency_ms"] for r in selected], .5),
                    "p95_ms": percentile([r["latency_ms"] for r in selected], .95),
                    "encoding_p50_ms": percentile([
                        r["response"]["metrics"]["encode_ms"] for r in selected], .5),
                    "engine_wait_and_model_p50_ms": percentile([
                        r["response"]["metrics"]["engine_wait_and_model_ms"] for r in selected], .5),
                })
    quality = evaluate_responses(fixtures, all_raw["quality"])
    confusion = Counter()
    per_replica = {}
    for row in all_raw["quality"]:
        if not row["valid"]:
            continue
        answer = row["response"]["answers"]["action"]["choice"]
        confusion[(row["expected"]["action"], answer)] += 1
        replica = row["response"]["metrics"]["replica_id"]
        tally = per_replica.setdefault(replica, {"valid": 0, "correct": 0})
        tally["valid"] += 1
        tally["correct"] += bool(row["correct"])
    quality["confusion"] = [
        {"expected": expected, "actual": actual, "count": count}
        for (expected, actual), count in sorted(confusion.items())
    ]
    quality["per_replica"] = per_replica
    quality["scenario_names"] = {
        row["id"]: row["description"]
        for row in read(ROOT / "experiments/promo-risk-20261121/scenarios.json")
    }
    qpass = quality["accuracy_all_attempts"] >= .9 and quality["failures"] == 0
    formal = [r for r in stages if r["name"] in ("confirm","repeat")]
    ppass = len(formal)==2 and all(r["passes_performance_acceptance"] for r in formal)
    pressure_pass = len(formal)==2 and all(r["correct"]/r["attempts"] >= .9 for r in formal)
    engines = sorted((EXP / "results/nodes").glob("*/replica-*/engine-config.json"))
    if len(engines) != 4:
        raise RuntimeError(f"Expected four engine proofs, found {len(engines)}")
    heads = set()
    for path in engines:
        config = read(path)
        assert config["settings"]["quantization"] == "fp8_per_channel"
        assert config["settings"]["max_num_seqs"] == 4
        assert config["settings"]["enable_prefix_caching"] is False
        for worker in config["workers"]:
            assert worker["readout_dtype"] == "torch.float32"
            assert len(worker["attention"]) == 8
            assert all("ENCODER_ONLY" in item["type"].upper() for item in worker["attention"])
            heads.add(worker["readout_sha256"])
    assert len(heads)==1
    environments = [read(p) for p in sorted((EXP / "results/nodes").glob("*/environment.json"))]
    assert len(environments) == 3
    for key in ("torch", "transformers", "vllm", "cuda", "karpo_source_sha256",
                "model_metadata_sha256", "model_weights_bytes", "adapter_sha256"):
        assert all(e[key] == environments[0][key] for e in environments), key
    weight_proofs = [read(p) for p in sorted((EXP / "results/setup").glob("weights-*.json"))]
    assert len(weight_proofs) == 3 and weight_proofs[0]["weights"]
    first_pressure = dt.datetime.fromisoformat(read(directory / "low-load/timing.json")["begin_utc"])
    for proof in weight_proofs:
        assert proof["weights"] == weight_proofs[0]["weights"], proof["host"]
        assert dt.datetime.fromisoformat(proof["ended_utc"]) < first_pressure, "Weight prefetch overlapped pressure test"
    prefetch_proofs = [read(p) for p in sorted((EXP / "results/setup").glob("prefetch-*.json"))]
    assert len(prefetch_proofs) == 2
    assert all(dt.datetime.fromisoformat(p["ended_utc"]) < first_pressure for p in prefetch_proofs)
    utilization = gpu_usage(directory)
    result = {
        "run": name, "dataset": "promo-risk-20261121", "model": "Karpo 4B FP8 / vLLM 0.34.0",
        "gpu": "4 RTX PRO 6000 Blackwell Server Edition 96GB",
        "quality_passed": qpass, "performance_passed": ppass, "pressure_quality_passed": pressure_pass,
        "accepted": qpass and ppass and pressure_pass, "quality": quality, "stages": stages,
        "gpu_utilization": utilization, "verified_requests": sum(map(len,all_raw.values())),
        "encoding": read(EXP / "results/encoding-summary.json"),
        "readout_sha256": next(iter(heads)), "engine_proofs": [str(p.relative_to(EXP)) for p in engines],
        "model_weights": weight_proofs[0]["weights"],
        "infrastructure": read(EXP / "operations/final-infrastructure.json") if (EXP / "operations/final-infrastructure.json").exists() else None,
        "scope": "Scenario-driven synthetic data; original full input. Actual measured HTTP path includes queue and routing; production feature lookup and entry network excluded.",
        "verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    assert result["accepted"] == status["accepted"]
    write(EXP / "data.json", result)
    write(EXP / "results/verification.json", {k:v for k,v in result.items() if k not in ("quality","stages","gpu_utilization")})
    write(EXP / "results/quality-details.json", quality)
    example = next((r for r in all_raw["quality"] if r["valid"] and not r["correct"]), all_raw["quality"][0])
    write(EXP / "results/decision-example.json", {
        "request_id": example["request_id"], "request": by_id[example["request_id"]]["request"],
        "expected": by_id[example["request_id"]]["expected"], "actual_response": example.get("response"),
        "latency_ms": example["latency_ms"], "source_stage": "quality",
    })
    render(result)
    print(json.dumps({k:result[k] for k in ("run","accepted","quality_passed","performance_passed",
                                          "pressure_quality_passed","verified_requests")},ensure_ascii=False))


def gpu_usage(directory):
    result = []
    for stage in ("confirm","repeat"):
        timing = read(directory / stage / "timing.json")
        start = dt.datetime.fromisoformat(timing["begin_utc"]) + dt.timedelta(seconds=30)
        end = dt.datetime.fromisoformat(timing["offered_end_utc"])
        for path in sorted((EXP / "results/nodes").glob("*/gpu.csv")):
            params = read(path.parent / "parameters.json")
            values = {replica:[] for replica in params["replica_ids"]}
            with path.open() as stream:
                for row in csv.DictReader(stream, skipinitialspace=True):
                    stamp = dt.datetime.strptime(row["timestamp"],"%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
                    if start <= stamp < end:
                        index = int(row["index"])
                        values[params["replica_ids"][index]].append(float(row["utilization.gpu [%]"].split()[0]))
            for replica, samples in values.items():
                if not samples:
                    raise RuntimeError(f"No utilization samples for {stage} replica {replica}")
                result.append({"stage":stage,"replica":replica,"samples":len(samples),
                               "mean_percent":sum(samples)/len(samples),"max_percent":max(samples)})
    return result


def render(data):
    q = data["quality"]
    decision = "通过" if data["accepted"] else "未通过"
    formal = [r for r in data["stages"] if r["name"] in ("confirm","repeat")]
    names = q["scenario_names"]
    f = lambda value: "—" if value is None else f"{value:.2f}"
    lines = [
        "# 新版薅羊毛风控：四RTX实际模型结果", "",
        f"**结论：{decision}本轮验收。** 质量{'通过' if data['quality_passed'] else '未通过'}，"
        f"性能{'通过' if data['performance_passed'] else '未通过'}，"
        f"压力正确率{'通过' if data['pressure_quality_passed'] else '未通过'}。", "",
        "使用完整200特征、原Karpo 4B、vLLM 0.34.0、FP8、每卡一个副本、最大活跃序列数4。"
        "质量540个案例各测两种候选顺序，共1,080请求；压力使用独立实体/种子的tune样例循环。"
        "两分区共享45种合成行为模板，不能代表真实业务效果。", "",
        "四张卡全部位于俄亥俄us-east-2c：原g7e.12xlarge提供两卡，"
        "两台g7e.2xlarge各提供一卡，共三台EC2。单卡实例因容量与配额约束换成较小CPU规格；"
        "压测客户端和代理位于双卡主机，通过同可用区私网访问另两台主机。", "",
        f"质量正确：{q['correct']}/{q['attempts']}（{100*q['accuracy_all_attempts']:.2f}%），"
        f"失败{q['failures']}；有效换序对{q['valid_order_pairs']}，其中{q['order_disagreements']}对选择不同。", "",
        f"正常行为被干预：{q['benign_interventions']}/{q['benign_attempts']}；"
        f"作弊行为被放行：{q['abuse_allowed']}/{q['abuse_attempts']}。"
        "失败另计，不能当作安全拦截；这些比例只针对本合成样例分布。", "",
        "| 正式阶段 | 到达RPS | 稳态有效RPS | P50 ms | P95 ms | 有效/尝试 | 正确/尝试 | 失败 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in formal:
        lines.append(f"| {row['name']} | 100 | {f(row['steady_valid_rps'])} | {f(row['p50_ms'])} | {f(row['p95_ms'])} | "
                     f"{row['valid']}/{row['attempts']} | {row['correct']}/{row['attempts']} | {row['failures']} |")
    wrong_allow = [r for r in q["confusion"] if r["expected"] != "allow" and r["actual"] == "allow"]
    if wrong_allow:
        lines += ["", "错误放行分布：" + "；".join(
            f"预期{r['expected']}、实际allow共{r['count']}条" for r in wrong_allow) + "。"]
    lines += ["", "本轮只验证四卡、最大活跃序列数4的配置。未扫描其他batch和卡数，"
              "因此不能将本配置的结果解释为四张RTX所有配置的性能上限。"
              "增加副本本身也不能解决低负载质量集上的决策错误。"]
    lines += ["", "正式阶段HTTP状态：" + "；".join(
        f"{r['name']} " + "、".join(f"{code}×{count}" for code, count in r["http_status_counts"].items())
        for r in formal) + "。429为队列满拒绝，502为代理上游HTTP错误；均计为失败。"]
    lines += ["", "端到端从预定到达到完整HTTP响应，包含排队、编码、推理、代理和实际实例链路。"
              "生产入口与在线特征查询不在范围内。全部失败计入；表中延迟为有效响应分位数。", "",
              f"输入为{data['encoding']['min_tokens']}～{data['encoding']['max_tokens']} token，"
              f"已核验{data['verified_requests']}次请求和四份GPU引擎配置；没有输入裁剪、规则代替模型或答案缓存。", "",
              "[完整数据](data.json) · [逐行为质量与错误](results/quality-details.json) · "
              "[完整输入及模型实际输出示例](results/decision-example.json) · "
              "[运行计划](PLAN.zh.md) · [原始数据说明](../promo-risk-20261121/README.zh.md)", "",
              "## 按预期动作统计", "", "| 预期动作 | 正确 | 请求 | 失败 |", "|---|---:|---:|---:|"]
    for action, row in sorted(q["groups"]["action"].items()):
        lines.append(f"| {action} | {row['correct']} | {row['attempts']} | {row['failures']} |")
    lines += ["", "## 实际决策混淆", "", "| 预期动作 | 实际动作 | 请求 |", "|---|---|---:|"]
    for row in q["confusion"]:
        lines.append(f"| {row['expected']} | {row['actual']} | {row['count']} |")
    lines += ["", "## 逐行为质量", "", "| 行为 | 正确 | 请求 | 失败 |", "|---|---:|---:|---:|"]
    for behavior, row in sorted(q["groups"]["behavior"].items()):
        lines.append(f"| {names.get(behavior, behavior)}（{behavior}） | {row['correct']} | {row['attempts']} | {row['failures']} |")
    lines += ["", "## GPU利用率", "", "| 正式阶段 | 副本 | 平均利用率 | 峰值利用率 |",
              "|---|---:|---:|---:|"]
    for row in data["gpu_utilization"]:
        lines.append(f"| {row['stage']} | {row['replica']} | {row['mean_percent']:.2f}% | {row['max_percent']:.0f}% |")
    if data["infrastructure"]:
        lines += ["", "## 实例状态", "", "资源状态以[最终核对记录](operations/final-infrastructure.json)中的时间为准。"]
        for row in data["infrastructure"]["instances"]:
            lines.append(f"- {row['id']} / {row['type']} / {row['az']} / {row['state']}")
    (EXP / "RESULTS.zh.md").write_text("\n".join(lines)+"\n")
    esc = html.escape
    table = "".join(f"<tr><td>{r['name']}</td><td>{f(r['steady_valid_rps'])}</td><td>{f(r['p50_ms'])}</td>"
                    f"<td>{f(r['p95_ms'])}</td><td>{r['correct']}/{r['attempts']}</td><td>{r['failures']}</td></tr>" for r in formal)
    behaviors = "".join(f"<tr><td>{esc(names.get(name, name))}<br><small>{esc(name)}</small></td><td>{r['correct']}/{r['attempts']}</td><td>{r['failures']}</td></tr>"
                        for name,r in sorted(q["groups"]["behavior"].items()))
    color = "#126332" if data["accepted"] else "#8d2c24"
    (EXP / "report.html").write_text(f"""<!doctype html><html lang="zh-CN">
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>新版薅羊毛风控 · 四RTX实际模型验证</title>
<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:1150px;margin:auto;padding:32px;background:#f5f7fb;color:#14243b}}
section{{background:white;padding:24px;margin:24px 0;border-radius:12px}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:10px;border-bottom:1px solid #dce4ef;text-align:left}}.scroll{{overflow:auto}}a{{color:#175ba3}}
.outcome{{color:{color};font-weight:bold;font-size:24px}}input{{font:inherit;padding:10px;width:90%}}[hidden]{{display:none}}</style>
<h1>薅羊毛风控：四RTX实际模型验证</h1><p class="outcome">{decision}本轮验收</p>
<section><h2>质量与性能分开验收</h2><p>质量正确率：{100*q['accuracy_all_attempts']:.2f}%
（{q['correct']}/{q['attempts']}）；有效换序对中{q['order_disagreements']}对输出不同。</p>
<p>正常行为被干预{q['benign_interventions']}/{q['benign_attempts']}；
作弊行为被放行{q['abuse_allowed']}/{q['abuse_attempts']}。这些是合成数据结果，不能代替生产验收。</p>
<p>完整200特征，Karpo 4B FP8，vLLM 0.34.0，4张同型号RTX、每卡独立模型副本。</p>
<p>全部位于俄亥俄us-east-2c：一台双卡g7e.12xlarge及两台单卡g7e.2xlarge。
压测客户端与代理位于双卡主机，另两台通过同可用区私网连接。</p>
<div class="scroll"><table><thead><tr><th>阶段</th><th>有效RPS</th><th>P50 ms</th><th>P95 ms</th><th>正确/全部</th><th>失败</th></tr></thead><tbody>{table}</tbody></table></div>
<p>每轮固定100 RPS、120秒。端到端包含排队及实际实例链路；不含生产入口和特征查询。</p></section>
<section><h2>原始证据</h2><p><a href="RESULTS.zh.md">详细文字报告</a> · <a href="data.json">全部指标及GPU利用率</a> ·
<a href="results/decision-example.json">完整输入与模型实际输出</a> ·
<a href="results/quality-details.json">错误明细</a> · <a href="PLAN.zh.md">冻结验收计划</a></p></section>
<section><h2>逐行为质量</h2><label for="q">按行为名称筛选</label><input id="q" type="search">
<div class="scroll"><table><thead><tr><th>行为</th><th>正确/全部</th><th>失败</th></tr></thead><tbody id="behaviors">{behaviors}</tbody></table></div></section>
<script>document.getElementById('q').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();
document.querySelectorAll('#behaviors tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}});</script></html>""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="rtx4-fp8-v1")
    build(parser.parse_args().name)
