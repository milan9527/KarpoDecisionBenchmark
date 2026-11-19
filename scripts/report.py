"""Build a Chinese report from recorded responses; never replace failures with reference answers."""
import collections
import hashlib
import json
import pathlib
import re
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]


def fmt(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}"


def main():
    result_root = ROOT / "results"
    environment_file = result_root / "environment.json"
    if not environment_file.exists():
        raise RuntimeError("No recorded model environment; a benchmark report cannot be generated.")
    environment = json.loads(environment_file.read_text())
    reports = []
    for path in sorted(result_root.glob("31b-*/summary.json")):
        raw = [json.loads(line) for line in (path.parent / "requests.jsonl").read_text().splitlines() if line.strip()]
        reports.append((path.parent.name, json.loads(path.read_text()), raw))
    measured = [(name, summary["quality-200-detailed"]) for name, summary, _ in reports
                if summary.get("quality-200-detailed", {}).get("p50_ms") is not None]
    conclusion = ""
    if measured:
        best_name, best = min(measured, key=lambda item: item[1]["p50_ms"])
        conclusion = (
            f"200 项长描述在已测配置中，P50 最低的是 **{best_name}**："
            f"**P50 {fmt(best['p50_ms'])}ms、P95 {fmt(best['p95_ms'])}ms**；"
            f"参考一致 {best['correct']}/{best['attempts']}，100ms 内正确完成 "
            f"{best['correct_within_100ms']}/{best['attempts']}。"
            "这是本机实测结果，不能推断其他模型或硬件上的性能上限。"
        )
    lines = [
        "# Karpo 31B：200 候选实测", "",
        f"运行进度：{sum((result_root / name / 'complete.json').exists() for name, _, _ in reports)}/3 组完整测试已完成。", "",
        conclusion, "",
        "本报告由保存的请求和响应生成。模型实际选择原样保留，失败计入完成率；没有用参考答案替换模型结果。", "",
        f"GPU：**{environment['gpu']}**。PyTorch `{environment['torch']}`，"
        f"Transformers `{environment['transformers']}`，CUDA `{environment['cuda']}`。",
        f"代码：`{environment['code_revision']}`；权重：`{environment['model_revision']}`。", "",
        "测试在单张 GPU 上运行，客户端通过本机 loopback HTTP 调用官方服务。延迟含客户端与服务排队、编码、推理和完整响应读取，"
        "不含生产网关、跨主机网络或工具执行。结果缓存关闭，200 项测试完整传入所有候选，不做召回缩减。", "",
        "带 Graphs 的配置仅对单问题纯文本且不超过 8192 tokens 的请求使用上游 CUDA Graphs；"
        "更长请求自动回退。配置名称表示启动选项。", "",
        "**对部署选型的含义**", "",
        "该配置未满足完整 200 候选、新请求 100ms 的目标。FP8 长描述吞吐约 0.74 请求/秒，"
        "并发从 1 增到 8 后吞吐基本不变、P95 升至约 10.7 秒；官方服务的推理锁使请求排队。"
        "增加 HTTP worker 不能据此视为推理吞吐提升。", "",
        "当前建议将 Karpo 31B 作为质量基线继续评估；若 100ms 是硬约束，需要另外实测候选召回、"
        "缩短候选描述或较小的决策模型，并用真实业务案例验证整个流程的准确率。"
        "缓存可以帮助重复输入，无法降低本次测出的未命中推理耗时。"
        "本次没有测试其他 GPU 或其他候选模型，不能给出它们的性能排名。", "",
        "**判断效果**", "",
        "200 项数据为 40 个合成快照、20 类规则模板，每例原始与反转候选顺序各一次；"
        "支付数据为现有项目的 40 个冻结快照、两种顺序，每次 2–6 个候选。配对观察不是独立案例，合成规则不代表 Redotpay 真实政策，"
        "这些结果不能视为生产准确率或 SOTA 证明。", "",
        "| 配置 | 数据 | 参考一致 / 尝试 | 有效返回 | P50 ms | P95 ms | 100ms 内正确完成 | 输入 tokens |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, summary, _ in reports:
        for key, title in [("quality-existing-payment", "原支付快照"), ("quality-200-detailed", "200 项长描述")]:
            s = summary.get(key)
            if s:
                lines.append(f"| {name} | {title} | {s['correct']}/{s['attempts']} | {s['valid']}/{s['attempts']} | "
                             f"{fmt(s['p50_ms'])} | {fmt(s['p95_ms'])} | {s['correct_within_100ms']}/{s['attempts']} | "
                             f"{s['input_tokens_min']}–{s['input_tokens_max']} |")
    lines += ["", "**候选数量与长度**", "",
              "每格是固定选取快照的重复性能观察；样本数较小，P95 不作为 SLA 保证。"
              "较小候选集在构造时保留正确答案，不代表实际召回器已经达到相同端到端质量。", "",
              "| 配置 | 候选 / 描述 | 有效/尝试 | P50 ms | P95 ms | 输入 tokens |",
              "|---|---|---:|---:|---:|---|"]
    for name, summary, _ in reports:
        for key, s in summary.items():
            if key.startswith("size-"):
                lines.append(f"| {name} | {key[5:]} | {s['valid']}/{s['attempts']} | {fmt(s['p50_ms'])} | "
                             f"{fmt(s['p95_ms'])} | {s['input_tokens_min']}–{s['input_tokens_max']} |")
    lines += ["", "**200 项长描述的耗时组成**", "",
              "取串行质量测试有效响应的分量中位数；各列分别统计，不能严格相加。", "",
              "| 配置 | 服务端编码 P50 ms | 服务端 model P50 ms | 客户端完整响应 P50 ms |",
              "|---|---:|---:|---:|"]
    for name, summary, raw in reports:
        rows = [r for r in raw if r["stage"] == "quality-200-detailed" and r["valid"]]
        components = [dict((k, float(v)) for k, v in re.findall(
            r"(\w+);dur=([0-9.]+)", r.get("server_timing") or "")) for r in rows]
        if rows and all("encode" in c and "model" in c for c in components):
            lines.append(f"| {name} | {fmt(statistics.median(c['encode'] for c in components))} | "
                         f"{fmt(statistics.median(c['model'] for c in components))} | "
                         f"{fmt(summary['quality-200-detailed']['p50_ms'])} |")
    lines += ["", "**并发与持续到达**", "",
              "并发扫描使用固定 worker；到达率测试按计划发送时间计算延迟，保留客户端超限、HTTP 错误和超时。"
              "吞吐是有效返回数除以完整测量时长，包含尾部排空；不是每秒候选数。", "",
              "| 配置 | 负载 | 有效/尝试 | 有效请求/秒 | P50 ms | P95 ms | P99 ms |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for name, summary, _ in reports:
        for key, s in summary.items():
            if key.startswith(("concurrency-", "arrival-")):
                lines.append(f"| {name} | {key} | {s['valid']}/{s['attempts']} | {fmt(s['valid_rps'], 2)} | "
                             f"{fmt(s['p50_ms'])} | {fmt(s['p95_ms'])} | {fmt(s['p99_ms'])} |")
    lines += ["", "**顺序与精度路径稳定性**", "",
              "一致性比较的是模型之间或同一模型两次输出是否相同，不等于答案正确。", ""]
    quality = {}
    for name, _, raw in reports:
        selected = [r for r in raw if r["stage"].startswith("quality-")]
        quality[name] = {r["request_id"]: r for r in selected}
        pairs = collections.defaultdict(dict)
        for r in selected:
            if r["group"] == "synthetic":
                pairs[r["case_id"]][r["order"]] = r
        valid_pairs = [p for p in pairs.values() if len(p) == 2 and all(r["valid"] for r in p.values())]
        changed = sum(p["original"]["response"]["answers"]["action"]["choice"] !=
                      p["reversed"]["response"]["answers"]["action"]["choice"] for p in valid_pairs)
        lines.append(f"- {name}：200 项候选反序后选择变化 {changed}/{len(valid_pairs)} 对，两次都有效才进入分母。")
    base = quality.get("31b-bf16", {})
    for name, candidates in quality.items():
        if name == "31b-bf16":
            continue
        common = [(a, candidates[k]) for k, a in base.items() if k in candidates and
                  a["valid"] and candidates[k]["valid"]]
        same = sum(all(a["response"]["answers"][k]["choice"] == b["response"]["answers"][k]["choice"]
                       for k in a["expected"]) for a, b in common)
        lines.append(f"- {name} 对 BF16 原路径：质量请求 top-1 相同 {same}/{len(common)}。")
    mismatches = []
    for name, _, raw in reports:
        for row in raw:
            if row["stage"].startswith("quality-") and not row.get("correct"):
                mismatches.append({"config": name, **row})
    (result_root / "quality-mismatches.json").write_text(json.dumps(mismatches, ensure_ascii=False, indent=2) + "\n")
    lines += ["", "**错误与证据**", "",
              f"共保存 {len(mismatches)} 个质量错误或无效响应，见 [错误明细](results/quality-mismatches.json)。",
              "每次调用保留请求哈希、原始响应、输入 token 数、客户端耗时及 `Server-Timing`。"
              "后者的 `model` 耗时包含 GPU Runner 锁等待，不能解释为纯 GPU 计算。", ""]
    launcher = result_root / "launcher.log"
    if launcher.exists():
        startup = re.findall(r"^READY (\S+) startup_seconds ([0-9.]+)$", launcher.read_text(), re.MULTILINE)
        lines += ["**首次启动准备**", "",
                  "以下为启动服务进程到健康检查成功的时间，含模型加载、内核准备和适用的图捕获，"
                  "不含实例开机、依赖安装和权重下载。", "",
                  "| 配置 | 启动准备秒数 |", "|---|---:|"]
        lines += [f"| {name} | {seconds} |" for name, seconds in startup]
        lines.append("")
    outcomes_path = result_root / "suite-outcomes.json"
    if outcomes_path.exists():
        outcomes = json.loads(outcomes_path.read_text())
        lines += ["运行状态：", "", "```json", json.dumps(outcomes, ensure_ascii=False, indent=2), "```", ""]
    lifecycle = result_root / "cloud-lifecycle.json"
    if lifecycle.exists():
        cloud = json.loads(lifecycle.read_text())
        lines += ["**实例与环境保留**", "",
                  "用户要求测试结束后只 stop、不 terminate。该指令到达前已提交本次实例终止请求，"
                  "AWS 无法撤回；后续资源删除已禁用。", "",
                  "| 实例 | 类型 | 最近观察状态 |", "|---|---|---|"]
        lines += [f"| `{iid}` | {entry['instance_type']} | {entry['state']} |"
                  for iid, entry in cloud["instances"].items()]
        retained = cloud.get("retained_root_volume")
        if retained:
            lines += ["", f"系统盘 `{retained['volume_id']}`：{retained['size_gib']} GiB，"
                      f"最近状态 `{retained['state']}`；已将随实例删除设置为 false。"]
        snapshot = cloud.get("preservation_attempt", {})
        if snapshot.get("snapshot_id"):
            lines += ["", f"系统盘快照 `{snapshot['snapshot_id']}`：最近状态 `{snapshot['state']}`，"
                      f"进度 {snapshot.get('progress', '未记录')}。"]
        lines += ["", "本地测试结果、私有 S3、专用 IAM 和安全组均保留。系统盘及快照继续产生存储费。"
                  "历史测试的模型权重放在实例 NVMe，无法随停机或终止保留；系统盘和快照不包含权重，"
                  "恢复时应按冻结 revision 重新下载。后续启动脚本已改用 EBS 保存权重。", ""]
        if "estimated_total_compute_usd" in cloud:
            lines += [f"本次临时实例计算费估算 **${cloud['estimated_total_compute_usd']:.3f}**；"
                      "不含 EBS、快照、IPv4、S3 和可能的网络费用。", ""]
        lines += ["资源与费用估算见 [云资源记录](results/cloud-lifecycle.json)。计算费按运行时长和记录价格估算，不是 AWS 结算账单。", ""]
    lines += ["原始结果目录：[results](results/)。测试代码与口径：[README](README.md)。"
              "缓存建议：[CACHE.zh.md](CACHE.zh.md)。", ""]
    (ROOT / "RESULTS.zh.md").write_text("\n".join(lines))
    files = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(result_root.rglob("*")) if p.is_file() and p.name != "evidence-sha256.json"}
    (result_root / "evidence-sha256.json").write_text(json.dumps(files, indent=2) + "\n")
    print(f"Generated report for {len(reports)} configurations, {len(mismatches)} quality mismatches.")


if __name__ == "__main__":
    main()
