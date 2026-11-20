"""Build a standalone, offline HTML report from the frozen benchmark evidence."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIGS = [
    ("31b-bf16", "BF16", "#a9b5c1"),
    ("31b-bf16-triton-graphs", "BF16 + Triton / Graphs", "#64778d"),
    ("31b-fp8-triton-graphs", "FP8 + Triton / Graphs", "#168178"),
]


def read_json(relative):
    return json.loads((ROOT / relative).read_text())


def esc(value):
    return html.escape(str(value), quote=True)


def number(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def table(headers, rows, caption):
    return (
        '<div class="table-wrap"><table><caption class="sr-only">'
        + esc(caption)
        + "</caption><thead><tr>"
        + "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def documentation_metadata(value):
    """Select operational metadata for reports while retaining raw evidence."""
    omitted = {
        "estimated_total_compute_usd", "estimated_compute_usd", "hourly_compute_usd",
        "usd_per_hour", "price_segments", "spot_history", "cost_scope",
        "estimate_notes", "estimated_seconds", "spot_price_cap", "result_cache",
    }
    if isinstance(value, dict):
        return {key: documentation_metadata(item) for key, item in value.items() if key not in omitted}
    if isinstance(value, list):
        return [documentation_metadata(item) for item in value]
    return value


def main():
    verification = read_json("results/verification.json")
    outcomes = read_json("results/suite-outcomes.json")
    if not verification["all_complete"] or any(c["status"] != "complete" for c in outcomes["configs"]):
        raise RuntimeError("Only completed, verified benchmark results can generate this report.")
    environment = read_json("results/environment.json")
    cloud = documentation_metadata(read_json("results/cloud-lifecycle.json"))
    configurations, raw = [], {}
    checks = {c["name"]: c for c in verification["checks"]}
    evidence_hashes = {}
    for name, label, color in CONFIGS:
        path = ROOT / f"results/{name}/requests.jsonl"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if checks[name]["requests_sha256"] != digest:
            raise RuntimeError(f"Recorded evidence hash changed: {name}")
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if len(rows) != checks[name]["recorded_rows"]:
            raise RuntimeError(f"Evidence row count changed: {name}")
        raw[name] = rows
        evidence_hashes[str(path.relative_to(ROOT))] = digest
        configurations.append({
            "id": name, "label": label, "color": color,
            "summary": read_json(f"results/{name}/summary.json"),
        })
    best = configurations[-1]["summary"]
    detailed = best["quality-200-detailed"]
    compact = best["size-200-compact"]
    concurrent = best["concurrency-1"]
    total_calls = sum(len(rows) for rows in raw.values())

    quality_rows, width_rows, concurrency_rows, arrival_rows, stability_rows = [], [], [], [], []
    reference = {r["request_id"]: r for r in raw["31b-bf16"] if r["stage"].startswith("quality-")}

    def choices(row):
        return {key: row["response"]["answers"][key]["choice"] for key in row["expected"]}

    for config in configurations:
        summary, label, name = config["summary"], config["label"], config["id"]
        for stage, title in [
            ("quality-200-detailed", "200 项 · 长描述"),
            ("quality-existing-payment", "支付快照 · 2–6 项"),
        ]:
            s = summary[stage]
            quality_rows.append([
                label, title, f'{s["correct"]}/{s["attempts"]}',
                f'{s["valid"]}/{s["attempts"]}', number(s["p50_ms"]), number(s["p95_ms"]),
                f'{s["correct_within_100ms"]}/{s["attempts"]}',
            ])
        for stage, s in summary.items():
            if stage.startswith("size-"):
                width_rows.append([
                    label, stage.split("-")[1], "短描述" if stage.endswith("compact") else "长描述",
                    f'{s["valid"]}/{s["attempts"]}', number(s["p50_ms"]), number(s["p95_ms"]),
                    f'{s["input_tokens_min"]:,}–{s["input_tokens_max"]:,}',
                ])
            if stage.startswith("concurrency-"):
                concurrency_rows.append([
                    label, stage.split("-")[1], f'{s["valid"]}/{s["attempts"]}',
                    number(s["valid_rps"], 3), number(s["p50_ms"]), number(s["p95_ms"]),
                ])
            if stage.startswith("arrival-"):
                arrival_rows.append([
                    label, stage.split("-")[1] + " 请求/秒",
                    f'{s["valid"]}/{s["attempts"]}', number(s["valid_rps"], 3),
                    number(s["p50_ms"]), number(s["p95_ms"]),
                ])
        quality = [r for r in raw[name] if r["stage"].startswith("quality-")]
        pairs = {}
        for row in quality:
            if row["group"] == "synthetic":
                pairs.setdefault(row["case_id"], {})[row["order"]] = row
        valid_pairs = [p for p in pairs.values() if len(p) == 2 and all(r["valid"] for r in p.values())]
        changed = sum(choices(p["original"]) != choices(p["reversed"]) for p in valid_pairs)
        common = [(r, reference[r["request_id"]]) for r in quality if r["valid"] and reference[r["request_id"]]["valid"]]
        same = sum(choices(a) == choices(b) for a, b in common)
        stability_rows.append([
            label, f"{changed}/{len(valid_pairs)} 对",
            "基线" if name == "31b-bf16" else f"{same}/{len(common)}",
        ])

    startup = re.findall(
        r"^READY (\S+) startup_seconds ([0-9.]+)$",
        (ROOT / "results/launcher.log").read_text(), re.MULTILINE,
    )
    labels = {c["id"]: c["label"] for c in configurations}
    startup_rows = [[labels[name], number(float(seconds), 2) + " 秒"] for name, seconds in startup]
    instance_rows = [[
        iid, s["instance_type"], s["region"], s["state"],
        s["observed_utc"][:19].replace("T", " ") + " UTC",
    ] for iid, s in cloud["instances"].items()]
    volume, snapshot = cloud["retained_root_volume"], cloud["preservation_attempt"]
    resource_rows = [
        ["系统盘", volume["volume_id"], f'{volume["size_gib"]} GiB · {volume["state"]}',
         volume["observed_utc"][:19].replace("T", " ") + " UTC"],
        ["系统盘快照", snapshot["snapshot_id"], f'{snapshot["state"]} · {snapshot.get("progress", "—")}',
         snapshot["observed_utc"][:19].replace("T", " ") + " UTC"],
    ]
    for path in [
        "results/environment.json", "results/cloud-lifecycle.json", "results/verification.json",
        "results/suite-outcomes.json", "fixtures/manifest.json",
        *[f'results/{c["id"]}/summary.json' for c in configurations],
    ]:
        evidence_hashes[path] = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    generated = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {
        "generated_utc": generated, "configurations": configurations,
        "environment": environment, "cloud": cloud, "verification": verification,
        "outcomes": outcomes, "evidence_sha256": evidence_hashes,
        "scope": {
            "network": "local loopback HTTP",
            "total_calls_including_warmup_and_repeats": total_calls,
            "synthetic_snapshots": 40, "synthetic_templates": 20, "orders_per_snapshot": 2,
            "existing_payment_snapshots": 40,
            "limitations": [
                "Synthetic policies do not establish Redotpay production accuracy or SOTA.",
                "Performance sweep cells contain 8 repeated observations each.",
                "Smaller candidate subsets preserve the gold option; retrieval recall was not tested.",
                "No finetuning or comparative run of other models was performed.",
                "Cloud resource states are timestamped observations, not a live console.",
            ],
        },
    }
    fields = {
        "DATE": esc(outcomes["ended_utc"][:10]),
        "GENERATED": esc(generated[:19].replace("T", " ") + " UTC"),
        "CALLS": number(total_calls, 0),
        "P50": number(detailed["p50_ms"], 0),
        "P95": number(detailed["p95_ms"], 0),
        "RATIO": number(detailed["p50_ms"] / 100),
        "COMPACT_P50": number(compact["p50_ms"], 1),
        "THROUGHPUT": number(concurrent["valid_rps"], 2),
        "C8_P95": number(best["concurrency-8"]["p95_ms"] / 1000, 2),
        "TORCH": esc(environment["torch"]),
        "TRANSFORMERS": esc(environment["transformers"]),
        "CUDA": esc(environment["cuda"]),
        "CODE_REVISION": esc(environment["code_revision"]),
        "MODEL_REVISION": esc(environment["model_revision"]),
        "QUALITY_TABLE": table(
            ["配置", "测试集", "参考一致", "有效返回", "P50 · ms", "P95 · ms", "100ms 内正确"],
            quality_rows, "各配置在两个质量测试集上的参考一致率与完整请求延迟"),
        "WIDTH_TABLE": table(
            ["配置", "候选数", "描述", "有效/尝试", "P50 · ms", "P95 · ms", "输入 tokens"],
            width_rows, "候选数量和描述长度的完整性能数据"),
        "CONCURRENCY_TABLE": table(
            ["配置", "并发数", "有效/尝试", "有效请求/秒", "P50 · ms", "P95 · ms"],
            concurrency_rows, "固定并发 worker 的完整结果"),
        "ARRIVAL_TABLE": table(
            ["配置", "计划到达率", "有效/尝试", "有效请求/秒", "P50 · ms", "P95 · ms"],
            arrival_rows, "固定到达率测试结果，延迟包含排队和尾部排空"),
        "STABILITY_TABLE": table(
            ["配置", "200 项换序后选择变化", "与 BF16 的质量请求 top-1 相同"],
            stability_rows, "候选顺序与精度路径稳定性"),
        "STARTUP_TABLE": table(["配置", "服务启动准备"], startup_rows, "进程启动至健康检查成功"),
        "INSTANCE_TABLE": table(["实例", "类型", "区域", "记录状态", "采集时间"], instance_rows, "实例历史状态"),
        "RESOURCE_TABLE": table(["资源", "ID", "记录状态", "采集时间"], resource_rows, "保留的系统盘与快照"),
        "DATA": json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"),
    }
    template = (ROOT / "scripts/report_template.html").read_text()
    output = re.sub(r"@@([A-Z0-9_]+)@@", lambda m: fields[m[1]], template)
    if re.search(r"@@[A-Z0-9_]+@@", output):
        raise RuntimeError("Unresolved template field")
    destination = ROOT / "report.html"
    destination.write_text(output)
    print(f"Generated {destination} ({len(output.encode()):,} bytes), {total_calls} verified calls.")


if __name__ == "__main__":
    main()
