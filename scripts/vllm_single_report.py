"""Verify raw evidence and produce the single-GPU comparison report."""

from collections import Counter
import datetime as dt
import hashlib
import html
import json
import math
from pathlib import Path
import sys

from benchmark import validate
from vllm_single_benchmark import summary

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-single-20261120"
RESULT = EXP / "results"


def read(path):
    return json.loads(path.read_text())


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def main():
    state = read(RESULT / "suite-status.json")
    assert state["status"] == "complete", state
    manifest = read(EXP / "manifest.json")
    archived_sources = []
    for path, expected in manifest["files"].items():
        source = EXP / "frozen" / path
        if source.exists():
            archived_sources.append(path)
        else:
            source = ROOT / path
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, path
    encodings = read(RESULT / "encoding/native.json")
    assert encodings == read(RESULT / "encoding/vllm.json")
    encoded = {r["request_id"]: r for r in encodings}
    fixtures = {
        item["id"]: item for scenario in ("risk", "sop")
        for item in read(EXP / f"{scenario}-requests.json")
    }
    attempts = valid = quality_errors = 0
    stage_count = c100_count = token_parity = 0
    for raw in sorted((RESULT / "measurements").rglob("requests.jsonl")):
        stage_count += 1
        stored = read(raw.with_name("summary.json"))
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        for row in rows:
            item = fixtures[row["request_id"]]
            assert item["request_sha256"] == row["request_sha256"]
            attempts += 1
            if row["valid"]:
                valid += 1
                assert validate(row["response"], item) == row["correct"]
                if raw.parent.name == "quality":
                    quality_errors += not row["correct"]
                response = row["response"]
                expected = encoded[item["id"]]
                assert response["usage"]["input_tokens"] == expected["length"]
                if response["metrics"].get("backend") == "vllm":
                    assert response["metrics"]["tokens_sha256"] == expected["tokens_sha256"]
                    assert response["metrics"]["marker"] == expected["marker"]
                    token_parity += 1
        fresh = summary(rows, stored["elapsed_seconds"])
        for key in ("attempts", "valid", "failures", "correct", "p50_ms", "p95_ms", "p99_ms", "valid_rps"):
            assert math.isclose(fresh[key], stored[key], rel_tol=1e-10, abs_tol=1e-8), (raw, key)
        if raw.parent.name == "sustained-100":
            c100_count += 1
            assert fresh["inflight"]["peak"] == 100
            assert fresh["inflight"]["mean_10_to_60s"] > 99
    assert c100_count == 12, c100_count
    assert valid == attempts
    postflight = read(RESULT / "postflight.json")
    if postflight["gpu_processes"].strip():
        settled = read(RESULT / "postflight-settled.json")
        assert settled["status"] == "empty" and not settled["samples"][-1]["gpu_processes"].strip()
    assert postflight["model_weights_bytes"] == 9081227392
    native_env, vllm_env = read(RESULT / "environment-native.json"), read(RESULT / "environment-vllm.json")
    assert native_env["karpo_source_sha256"] == vllm_env["karpo_source_sha256"]
    assert native_env["model_metadata_sha256"] == vllm_env["model_metadata_sha256"]
    selection = read(RESULT / "selection.json")
    comparisons = []
    matrix = []
    for scenario in ("risk", "sop"):
        def metric(name, stage="sustained-100"):
            return read(RESULT / "measurements" / name / scenario / stage / "summary.json")

        initial, repeated = metric("native-initial"), metric("native-confirm")
        chosen = selection[scenario]["selected"]
        confirm_name = "vllm-confirm" if (RESULT / "measurements/vllm-confirm").exists() else f"vllm-confirm-{scenario}"
        confirmed = metric(confirm_name)
        quality_path = RESULT / "measurements" / f"vllm-b{chosen['batch']}" / scenario / "quality"
        comparison = {
            "scenario": scenario,
            "native_initial": initial, "native_confirm": repeated,
            "native_quality": metric("native-initial", "quality"),
            "vllm_batch": chosen["batch"], "vllm_initial": chosen["performance"],
            "vllm_confirm": confirmed, "vllm_quality": read(quality_path / "summary.json"),
            "parity": read(quality_path / "parity-vs-reference.json"),
            "parity_vs_historical_bf16": read(quality_path / "parity-vs-historical-bf16.json"),
            "p50_speedup_vs_native_confirm": repeated["p50_ms"] / confirmed["p50_ms"],
            "rps_ratio_vs_native_confirm": confirmed["valid_rps"] / repeated["valid_rps"],
            "native_repeat_p50_ratio": repeated["p50_ms"] / initial["p50_ms"],
            "passes_latency": confirmed["p50_ms"] <= 500,
        }
        comparisons.append(comparison)
        for name in ("native-initial", "vllm-b1", "vllm-b4", "vllm-b8"):
            matrix.append({
                "scenario": scenario, "name": name,
                "performance": metric(name),
                "serial": metric(name, "serial"),
                "quality": metric(name, "quality"),
            })
    verification = {
        "status": "complete", "verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stages": stage_count, "sustained_c100_runs": c100_count,
        "http_attempts": attempts, "valid_http_responses": valid, "failures": attempts - valid,
        "quality_error_observations": quality_errors, "identical_encoded_fixtures": len(encodings),
        "verified_vllm_response_tokens": token_parity,
        "frozen_files_verified": len(manifest["files"]),
        "archived_sources_verified": archived_sources,
    }
    dump(RESULT / "verification.json", verification)
    data = {
        "experiment": EXP.name, "comparisons": comparisons, "matrix": matrix, "verification": verification,
        "environment": {"native": native_env, "vllm": vllm_env},
        "resource_state": read(RESULT / "infrastructure-final.json") if (RESULT / "infrastructure-final.json").exists() else None,
    }
    dump(EXP / "data.json", data)
    labels = {"risk": "风控", "sop": "SOP"}
    lines = [
        "# Karpo 4B：vLLM 与原生 FP8 单卡对照", "",
        "使用同一张 RTX PRO 6000 Blackwell Server Edition 96GB（AWS g7e.4xlarge），两套服务顺序运行。"
        "原始输入、完整选项、Karpo 模板及 marker 后的 9 个模板 token 全部保留。", "",
        "验收口径：持续 100 个在途请求，完整 HTTP P50 ≤500ms；风控与 SOP 分开。"
        "下表使用各自独立复测结果，吞吐分母包含最终排空。", "",
        "| 场景 | 原生 P50 | vLLM P50 | P50 加速比 | 原生 RPS | vLLM RPS | vLLM 最大序列数 | 达标 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for c in comparisons:
        n, v = c["native_confirm"], c["vllm_confirm"]
        lines.append(f"| {labels[c['scenario']]} | {n['p50_ms']:.1f}ms | {v['p50_ms']:.1f}ms | "
                     f"{c['p50_speedup_vs_native_confirm']:.2f}× | {n['valid_rps']:.2f} | {v['valid_rps']:.2f} | "
                     f"{c['vllm_batch']} | {'是' if c['passes_latency'] else '否'} |")
    lines += ["", "## 质量与一致性", "",
              "| 场景 | 原生合成正确率 | vLLM 合成正确率 | 同答案 | 新增错误 | 最大概率差 |",
              "|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        q, n, p = c["vllm_quality"], c["native_quality"], c["parity"]
        lines.append(f"| {labels[c['scenario']]} | {n['correct']}/{n['attempts']} "
                     f"({n['accuracy_all_attempts']:.2%}) | {q['correct']}/{q['attempts']} "
                     f"({q['accuracy_all_attempts']:.2%}) | {p['same_choices']}/{p['observations']} | "
                     f"{p['new_errors']} | {p['max_probability_delta']:.6f} |")
    lines += [
        "", "合成正确率只反映已有策略样例的一致性，不代表生产准确率。选择配置只使用性能和预先约定的 >90% 质量门槛。",
        "", "## 全部配置", "",
        "| 场景 | 配置 | C1 P50 | C100 P50 | C100 P95 | RPS | 合成正确率 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for c in matrix:
        v, q = c["performance"], c["quality"]
        lines.append(f"| {labels[c['scenario']]} | {c['name']} | {c['serial']['p50_ms']:.1f}ms | "
                     f"{v['p50_ms']:.1f}ms | {v['p95_ms']:.1f}ms | {v['valid_rps']:.2f} | "
                     f"{q['accuracy_all_attempts']:.2%} |")
    lines += [
        "", "## 实现与边界", "",
        "- 专用 vLLM 插件加载原始 Qwen3.5 权重及 readout；全注意力双向、GatedDeltaNet 保持因果。"
        "按 Karpo marker 位置读取，保留完整序列；输出头以 FP32 运行，其权重哈希逐次启动核对。",
        "- vLLM 0.34.0 使用逐通道 FP8 权重和逐 token 动态激活；小型门控层不量化。"
        "原生 FP8 的 kernel 与舍入不同，因此报告逐例答案和概率差异。",
        "- 关闭前缀缓存、chunked prefill；max_model_len=8192；token budget 上限 65536。"
        "按输入长度配置 prefill CUDA Graph，两套服务都计入编码和调度开销。",
        f"- 原生 Torch={native_env['torch']}；vLLM Torch={vllm_env['torch']}；"
        f"Transformers={vllm_env['transformers']}。比较完整服务方案，不能将全部收益归因于 GPU kernel。",
        "- 延迟不含生产网络入口及特征查询；P95/P99 仅观察。max_num_seqs 是 GPU 调度配置，"
        "HTTP 在途并发独立固定为 100。",
        "- vLLM 的显存占用包含 gpu_memory_utilization=0.8 的缓存预留；本次未测试最低显存配置。",
        "- 仅支持本实验单问题纯文本请求；其他输入明确拒绝。保留既有模型、EBS 和原生环境。",
        "", "## 证据", "",
        f"- {attempts:,} 次 HTTP 请求全部有效；12 次持续 C100 测试；464 条编码全部一致。",
        "- 原始日志：results/measurements；逐例答案差异：各配置 quality/parity-vs-reference.json。",
        "- GPU 和软件证据：results/gpu.csv、environment-*.json、servers/*/engine-config.json。",
        "- 复现配置：manifest.json、PLAN.zh.md；开发启动日志单独保留，不计入性能。",
    ]
    if data["resource_state"]:
        lines += ["- 最终云资源状态：results/infrastructure-final.json。"]
    (EXP / "RESULTS.zh.md").write_text("\n".join(lines) + "\n")
    cards = ""
    for c in comparisons:
        v = c["vllm_confirm"]
        cards += f"""<article><small>{labels[c['scenario']]} · vLLM 最多{c['vllm_batch']}序列 · 独立复测</small>
<strong>{v['p50_ms']:,.0f}<span> ms</span></strong>
<p>完整 HTTP P50 · 持续100并发</p><b>{v['valid_rps']:.1f} 请求/秒</b>
<p>原生复测 P50：{c['native_confirm']['p50_ms']:,.0f} ms</p>
<p>相对原生复测 {c['p50_speedup_vs_native_confirm']:.2f}× · {'达到' if c['passes_latency'] else '未达到'}500ms</p></article>"""
    table = "".join(
        f"<tr><td>{labels[c['scenario']]}</td><td>{html.escape(c['name'])}</td>"
        f"<td>{c['serial']['p50_ms']:,.1f}</td><td>{c['performance']['p50_ms']:,.1f}</td>"
        f"<td>{c['performance']['p95_ms']:,.1f}</td><td>{c['performance']['valid_rps']:.2f}</td>"
        f"<td>{c['quality']['accuracy_all_attempts']:.2%}</td></tr>" for c in matrix
    )
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Karpo · vLLM 单卡对照</title>
<style>body{{margin:0;background:#f4f6f9;color:#17263a;font:16px/1.65 system-ui,sans-serif}}
main{{max-width:1080px;margin:auto;padding:40px 22px}}h1{{font-size:clamp(26px,4vw,42px);line-height:1.2}}
.eyebrow{{color:#376898;font-size:13px;letter-spacing:2px}}.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:18px;margin:30px 0}}
article{{background:white;border:1px solid #dbe2eb;border-radius:16px;padding:24px}}
article strong{{display:block;font-size:50px;line-height:1.6;color:#174c7a}}article span{{font-size:20px}}
small,p{{color:#526478}}.table{{overflow:auto;background:white;border:1px solid #dbe2eb;border-radius:12px}}
table{{border-collapse:collapse;width:100%;white-space:nowrap;text-align:right}}th,td{{padding:14px;border-bottom:1px solid #e5ebf1}}
th:first-child,td:first-child,td:nth-child(2){{text-align:left}}th{{background:#edf2f7}}
.note{{border-left:4px solid #376898;padding:8px 20px;margin:26px 0}}a{{color:#175c93}}
@media(max-width:640px){{.cards{{grid-template-columns:1fr}}main{{padding:24px 16px}}}}</style>
<main><div class="eyebrow">KARPO DECISION BENCHMARK / 2026-11-20</div>
<h1>vLLM 与原生 FP8<br>同一张 GPU，完整原始输入</h1>
<p>RTX PRO 6000 Blackwell Server Edition · 96GB · AWS g7e.4xlarge<br>风控与 SOP 分别验证，验收目标为持续100并发、完整 HTTP P50 ≤500ms。</p>
<section class="cards">{cards}</section><h2>全部单卡配置</h2>
<div class="table"><table><thead><tr><th>场景</th><th>配置</th><th>C1 P50 / ms</th><th>C100 P50 / ms</th><th>P95 / ms</th><th>请求/秒</th><th>合成正确率</th></tr></thead><tbody>{table}</tbody></table></div>
<div class="note"><b>输入与决策语义</b><p>464 条请求的 token 和 marker 一致；完整保留200特征／200选项以及 marker 后的模板 token。关闭前缀缓存和分块 prefill，原始 FP32 输出头权重逐次核对。</p></div>
<p>{attempts:,} 次 HTTP 请求有效，12 次持续并发测试均还原检查在途请求。合成正确率不代表生产准确率；时延不含生产网络及特征查询。服务前端和 Torch 版本差异包含在比较中。</p>
<p><a href="RESULTS.zh.md">完整中文报告</a> · <a href="data.json">结构化结果</a> · <a href="results/verification.json">验证记录</a> · <a href="PLAN.zh.md">实验方案</a></p></main></html>"""
    (EXP / "report.html").write_text(page)
    hashes = {
        str(p.relative_to(EXP)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(EXP.rglob("*"))
        if p.is_file() and p.name != "evidence-sha256.json"
    }
    dump(RESULT / "evidence-sha256.json", hashes)
    print(json.dumps(verification))


if __name__ == "__main__":
    main()
