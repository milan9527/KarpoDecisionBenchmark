"""Verify raw full-200-candidate responses and generate an experimental report."""
import collections
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re

from benchmark import summarize, validate
from report_common import documentation_metadata, table
from prepare import digest
from family_status_report import notice as family_notice
from latest_report import family_complete

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/concise-fp8-20261119"
LABELS = {
    "original": "原始长描述",
    "concise-en": "英文精简",
    "concise-zh": "中文精简",
    "factored": "公共规则 + 候选引用",
}


def load(path):
    return json.loads(path.read_text())


def f(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def main():
    manifest = load(EXP / "fixtures/manifest.json")
    # Narrative documents can change after the run; verify their original bytes.
    frozen_documents = load(EXP / "results/frozen-documents.json")
    for name, expected in load(EXP / "inputs-sha256.json")["files"].items():
        original = frozen_documents[name].encode() if name in frozen_documents else (ROOT / name).read_bytes()
        assert hashlib.sha256(original).hexdigest() == expected, name
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((EXP / name).read_bytes()).hexdigest() == expected, name
    fixture = {row["id"]: row for row in load(EXP / "fixtures/requests.json")}
    status = load(EXP / "results/suite-status.json")
    outcomes = load(EXP / "results/outcomes.json")
    assert status["status"] == "complete", status
    assert len(outcomes["variants"]) == 4
    assert all(row["status"] == "complete" for row in outcomes["variants"])
    reports, raw, quality_rows, concurrency_rows, mismatches, verified = {}, {}, [], [], [], []
    stage_counts = {
        "warmup-excluded": 8, "quality-existing": 80, "quality-boundary": 24,
        **{f"concurrency-{n}": 24 for n in [1, 2, 4, 8]},
    }
    for variant in manifest["variants"]:
        directory = EXP / "results" / variant
        path = directory / "requests.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        raw[variant] = rows
        summary = load(directory / "summary.json")
        reports[variant] = summary
        expected_counts = dict(stage_counts)
        if variant == "original":
            expected_counts["baseline-drift-after"] = 16
        assert collections.Counter(r["stage"] for r in rows) == expected_counts
        assert len(rows) == load(directory / "complete.json")["observations"]
        assert sorted(r["sequence"] for r in rows) == list(range(1, len(rows) + 1))
        for row in rows:
            request = fixture[row["request_id"]]
            assert request["description"] == variant
            for key in ["case_id", "group", "order", "description"]:
                assert row[key] == request[key], (variant, key)
            assert row["request_sha256"] == digest(request["request"]) == request["request_sha256"]
            assert row["expected"] == request["expected"]
            assert row["candidate_count"] == len(request["request"]["questions"]["action"]["criteria"]) == 200
            if row["valid"]:
                assert row["correct"] == validate(row["response"], request)
            if row["stage"].startswith("quality-") and not row["correct"]:
                mismatches.append({"variant": variant, "state": json.loads(request["request"]["state"]), **row})
        for stage, size in expected_counts.items():
            subset = [r for r in rows if r["stage"] == stage]
            recalculated = summarize(subset, summary[stage]["elapsed_seconds"])
            assert recalculated == summary[stage], (variant, stage, "summary differs from raw")
            if stage.startswith("quality-"):
                selected_ids = [r["request_id"] for r in subset]
                assert len(set(selected_ids)) == size, (variant, stage, "quality request duplicated")
                group = "synthetic" if stage == "quality-existing" else "boundary"
                expected_ids = {r["id"] for r in fixture.values()
                                if r["description"] == variant and r["group"] == group}
                assert set(selected_ids) == expected_ids, (variant, stage, "quality coverage")
        for stage, label in [("quality-existing", "原有40例 × 两序"), ("quality-boundary", "新增12例 × 两序")]:
            s = summary[stage]
            quality_rows.append([
                LABELS[variant], label, f'{s["correct"]}/{s["attempts"]}',
                f'{s["valid"]}/{s["attempts"]}', f(s["p50_ms"]), f(s["p95_ms"]),
                f'{s["input_tokens_min"]:,}–{s["input_tokens_max"]:,}',
            ])
        for n in [1, 2, 4, 8]:
            s = summary[f"concurrency-{n}"]
            concurrency_rows.append([
                LABELS[variant], n, f'{s["valid"]}/{s["attempts"]}',
                f(s["valid_rps"], 3), f(s["p50_ms"]), f(s["p95_ms"]),
            ])
        verified.append({"variant": variant, "observations": len(rows),
                         "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    total = sum(len(rows) for rows in raw.values())
    assert total == 848, total
    (EXP / "results/verification.json").write_text(json.dumps({
        "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "all_complete": True, "total_calls": total, "checks": verified,
        "invariants": "200 candidates, frozen request hashes, exact quality coverage, raw answers and stage metrics verified.",
    }, indent=2) + "\n")
    (EXP / "results/quality-errors.json").write_text(
        json.dumps(mismatches, ensure_ascii=False, indent=2) + "\n")
    best = min(reports, key=lambda v: reports[v]["quality-existing"]["p50_ms"])
    base = reports["original"]["quality-existing"]
    fastest = reports[best]["quality-existing"]
    ratio = fastest["p50_ms"] / base["p50_ms"]
    boundary = reports[best]["quality-boundary"]
    passed = fastest["correct"] == 80 and boundary["correct"] == 24
    conclusion = (
        f"完整保留 200 个候选，最快的是「{LABELS[best]}」：原有场景 P50 {f(fastest['p50_ms'])}ms、"
        f"P95 {f(fastest['p95_ms'])}ms，比同机原始长描述的 P50 减少 {f((1-ratio)*100)}%。"
        f"该组原有场景参考一致 {fastest['correct']}/80，新增边界观察 {boundary['correct']}/24。"
    )
    if not passed:
        conclusion += " 最快组存在选择错误，不能只依据速度把它作为推荐方案。"
    if fastest["p50_ms"] > 100:
        conclusion += " 最快组P50仍高于100ms目标。"
    stability_rows = []
    baseline = {(r["case_id"], r["order"]): r for r in raw["original"] if r["stage"].startswith("quality-")}
    for variant, rows in raw.items():
        quality = [r for r in rows if r["stage"].startswith("quality-")]
        pairs = collections.defaultdict(dict)
        for r in quality:
            pairs[r["case_id"]][r["order"]] = r
        valid_pairs = [p for p in pairs.values() if len(p) == 2 and all(r["valid"] for r in p.values())]
        choice = lambda r: r["response"]["answers"]["action"]["choice"]
        changes = sum(choice(p["original"]) != choice(p["reversed"]) for p in valid_pairs)
        comparable = [(r, baseline[r["case_id"], r["order"]]) for r in quality
                      if r["valid"] and baseline[r["case_id"], r["order"]]["valid"]]
        same = sum(choice(a) == choice(b) for a, b in comparable)
        stability_rows.append([LABELS[variant], f"{changes}/{len(valid_pairs)} 对",
                               "基线" if variant == "original" else f"{same}/{len(comparable)}"])
    preserving = [
        variant for variant, rows in raw.items()
        if all(not baseline[r["case_id"], r["order"]]["correct"] or r["correct"]
               for r in rows if r["stage"].startswith("quality-"))
    ]
    preferred = min(preserving, key=lambda v: reports[v]["quality-existing"]["p50_ms"])
    if preferred != "original":
        remaining_errors = sum(not r["correct"] for r in raw[preferred]
                               if r["stage"].startswith("quality-"))
        conclusion += (
            f" 建议以「{LABELS[preferred]}」作为下一轮优化起点："
            "本批观察中，基线已答对的用例没有新增错误，"
            f"但仍有 {remaining_errors}/104 条质量观察错误。"
        )
    before = reports["original"]["concurrency-1"]["p50_ms"]
    after = reports["original"]["baseline-drift-after"]["p50_ms"]
    change = (after / before - 1) * 100
    change_text = "不足0.1%" if abs(change) < 0.1 else f"{f(change)}%"
    drift = f"同一组8个输入，基线并发1的P50 {f(before)}ms；结束复测P50 {f(after)}ms，变化{change_text}。"
    cloud_path = EXP / "results/infrastructure.json"
    cloud = documentation_metadata(load(cloud_path)) if cloud_path.exists() else {}
    runtime = load(EXP / "results/bootstrap.json")["environment"]
    tables = [
        ("质量与延迟", ["描述", "数据", "参考一致", "有效/尝试", "P50 ms", "P95 ms", "输入tokens"], quality_rows),
        ("候选顺序与选择一致性", ["描述", "换序后选择变化", "与同轮原始描述选择相同"], stability_rows),
        ("并发", ["描述", "并发", "有效/尝试", "有效请求/秒", "P50 ms", "P95 ms"], concurrency_rows),
    ]
    if mismatches:
        catalog = {c["id"]: c for c in load(ROOT / "fixtures/catalog.json")}
        errors_by_choice = collections.defaultdict(list)
        for row in mismatches:
            chosen = row.get("response", {}).get("answers", {}).get("action", {}).get("choice")
            errors_by_choice[row["variant"], row["case_id"], row["expected"]["action"], chosen].append(row["order"])

        def action_name(candidate):
            if candidate not in catalog:
                return candidate or "无有效选择"
            return catalog[candidate]["compact"].split(": ", 1)[1].split(" / ", 1)[0]

        error_rows = [[LABELS[v], case, "、".join("原序" if o == "original" else "反序" for o in orders),
                       action_name(expected), action_name(chosen)]
                      for (v, case, expected, chosen), orders in errors_by_choice.items()]
        tables.append(("错误明细", ["描述", "用例", "候选顺序", "参考动作", "实际动作"], error_rows))
    method = (
        "模型一次直接选择，程序不做业务域路由或筛选；四组保留相同状态、200个候选ID和候选顺序。"
        "所有参考答案在调用前冻结，答案不进入模型输入。原有40例与新增12例都是合成场景，各测两种顺序；"
        "配对观察和重复压测不是新增独立案例，不能证明Redotpay生产准确率或SOTA。"
        "公共规则版本利用当前目录10个域×20类规则的重复结构；不能推断200条完全不同的SOP也能同比压缩。"
    )
    timing = (
        "同一FP8 + Triton / Graphs服务，本机loopback HTTP完整响应计时。"
        "超过8192tokens的请求自动回退到非Graphs路径；本实验测量描述精简在该启动配置下的整体效果，"
        "无法分别归因于token减少或Graphs。每组预热8次不计正式延迟，并发每格24次属于短时扫描。"
    )
    lines = ["# 全部200候选：精简描述 + FP8 实测", "", conclusion, "", method, "", timing, ""]
    for title, headers, rows in tables:
        lines += [f"## {title}", "", "| " + " | ".join(headers) + " |",
                  "|" + "|".join(["---"] * len(headers)) + "|"]
        lines += ["| " + " | ".join(map(str, row)) + " |" for row in rows]
        lines.append("")
    lines += ["## 复测与错误", "", drift, "",
              f"质量错误或无效观察共 {len(mismatches)} 条，完整输入状态及原始响应见 [quality-errors.json](results/quality-errors.json)。", "",
              f"本轮记录 {total} 次调用，含32次预热、384次并发观察和16次结束基线复测。", "",
              f"服务启动准备 {f(status['startup_seconds'],2)} 秒；GPU：{runtime['gpu']}。", "",
              "## 实例保留", "", "本轮使用 persistent Spot，关机及回收行为均为 stop，系统盘随实例删除为 false。"]
    if cloud:
        lines += ["", f"实例 `{cloud['instance_id']}`，最近状态 **{cloud['state']}**；"
                  f"观察时间 {cloud['observed_utc']}。模型权重保存在 EBS。"]
    lines += ["", "四组冻结描述：[description-variants.json](fixtures/description-variants.json)。"
              "计划：[PLAN.zh.md](PLAN.zh.md)。逐条响应与汇总：[results](results/)。", ""]
    markdown = "\n".join(lines)
    (EXP / "RESULTS.zh.md").write_text(markdown)
    # The main entry points show the same verified run, with links rebased.
    relative_experiment = EXP.relative_to(ROOT).as_posix()
    main_markdown = markdown.replace("](results/", f"]({relative_experiment}/results/")
    main_markdown = main_markdown.replace("](fixtures/", f"]({relative_experiment}/fixtures/")
    main_markdown = main_markdown.replace("](PLAN.zh.md)", f"]({relative_experiment}/PLAN.zh.md)")
    if family_notice(ROOT / "report.html"):
        main_markdown = (
            "> 2026-11-20 新实验尚未执行：12B / 9B / 4B 的测试已准备，"
            "AWS 容量不足，新增模型调用为 0。"
            "[查看实际进度](experiments/family-20261120/STATUS.zh.md)。"
            "下方为 11 月 19 日已完成的 31B 描述实验。\n\n" + main_markdown
        )
    if not family_complete():
        (ROOT / "RESULTS.zh.md").write_text(main_markdown)
    css = (ROOT / "scripts/report.css").read_text()
    esc = html.escape
    table_html = "".join(
        f'<section id="part-{i}"><div class="section-head"><div class="section-title">'
        f'<span class="section-num">0{i+1}</span><h2>{esc(title)}</h2></div></div>'
        f'<div class="panel">{table(headers, rows, title)}</div></section>'
        for i, (title, headers, rows) in enumerate(tables))
    payload = json.dumps(documentation_metadata({
        "variants": [{"id": v, "label": LABELS[v], "summary": reports[v]} for v in manifest["variants"]],
        "infrastructure": cloud, "manifest": documentation_metadata(manifest), "verification": verified,
    }), ensure_ascii=False).replace("<", "\\u003c")
    cards = "".join(
        f'<div class="kpi{" accent" if v == preferred else ""}">'
        f'<div class="kpi-top">{esc(LABELS[v])} · P50</div>'
        f'<div class="kpi-value">{f(reports[v]["quality-existing"]["p50_ms"])}<small>ms</small></div>'
        f'<div class="kpi-note">原有场景 {reports[v]["quality-existing"]["correct"]}/80'
        f' · 边界 {reports[v]["quality-boundary"]["correct"]}/24</div></div>'
        for v in manifest["variants"]
    )
    infrastructure = esc(json.dumps(cloud, ensure_ascii=False, indent=2)) if cloud else "资源状态稍后补充。"
    html_page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>200候选 · 描述精简 + FP8</title>
<style>{css}</style></head><body>
<aside class="sidebar"><a class="brand" href="#overview"><span class="brand-mark">K</span> Karpo Decision</a>
<div class="side-label">Full 200 candidates / FP8</div><nav class="nav"><a href="#overview">结论与延迟</a>
<a href="#part-0">选择质量</a><a href="#part-1">顺序稳定性</a><a href="#part-2">并发</a><a href="#part-3">错误明细</a>
<a href="#methods">测试口径与资源</a></nav><div class="side-bottom">全部200项交给模型<br>无程序路由 · 无候选过滤</div></aside>
<main><div class="topbar"><div class="crumb">Karpo 31B / 描述精简实验</div><div class="top-actions">
<button class="button" id="download">下载汇总数据</button><button class="button primary" onclick="window.print()">打印 / PDF</button></div></div>
<section id="overview"><div class="eyebrow">200 CANDIDATES · FOUR DESCRIPTIONS · SAME MODEL</div>
<h1 style="font-size:clamp(26px,3vw,36px);line-height:1.4;margin:16px 0">全部200项，<br>精简描述能加速多少？</h1>
<p class="text-block" style="font-size:14px;max-width:920px">{esc(conclusion)}</p>
<div class="pills"><span class="pill green">4/4组完成</span><span class="pill">848次调用，含预热与重复</span>
<span class="pill">RTX PRO 6000 · 96GB</span><span class="pill">同一FP8服务</span></div>
<div class="kpis">{cards}</div>
<div class="panel panel-padding"><div class="panel-header"><div><h3>原有40例 × 两种顺序</h3>
<p class="panel-sub">零起点线性刻度 · 全部200候选 · 完整请求延迟</p></div>
<div class="segmented controls"><button data-metric="p50_ms" aria-pressed="true">P50</button>
<button data-metric="p95_ms" aria-pressed="false">P95</button></div></div>
<div class="legend"><span><span class="goal-key"></span>100ms目标</span></div><div id="bars" class="bar-chart"></div>
<div class="chart-note">{esc(drift)}</div></div></section>
{table_html}<section id="methods"><div class="section-head"><h2>测试口径与资源保留</h2></div>
<div class="panel panel-padding text-block"><p>{esc(method)}</p><p>{esc(timing)}</p>
<p>质量错误或无效观察共 {len(mismatches)} 条；查看<a href="@@ERRORS@@">每条错误及原始响应</a>。</p>
<p>模型权重保存在EBS。本轮结束只stop，不terminate；下方是带时间的资源记录。</p>
<details><summary>查看资源状态</summary><pre class="code-line" style="white-space:pre-wrap">{infrastructure}</pre></details></div></section>
<footer class="footer"><span>2026-11-19 · 单文件离线报告</span><div class="footer-links">
<a href="@@RESULTS@@">Markdown</a><a href="@@PLAN@@">冻结计划</a><a href="@@README@@">复现说明</a></div></footer></main>
<script type="application/json" id="data">{payload}</script>"""
    html_page += r"""<script>
const data=JSON.parse(document.getElementById('data').textContent);
const fmt=n=>n.toLocaleString('zh-CN',{minimumFractionDigits:1,maximumFractionDigits:1});
function render(metric){
 const max=Math.ceil(Math.max(...data.variants.map(v=>v.summary['quality-existing'][metric]))/500)*500;
 document.getElementById('bars').innerHTML=data.variants.map((v,i)=>{
  const value=v.summary['quality-existing'][metric];
  return `<div class="bar-row"><div class="bar-label"><b>${v.label}</b></div><div class="bar-track"><div class="bar-fill" style="width:${value/max*100}%;background:${i?'#168178':'#a9b5c1'}"></div><span class="bar-goal" style="left:${100/max*100}%" aria-hidden="true"></span></div><div class="bar-value">${fmt(value)} <small>ms</small></div></div>`;
 }).join('');
}
document.querySelectorAll('[data-metric]').forEach(b=>b.addEventListener('click',()=>{
 document.querySelectorAll('[data-metric]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));render(b.dataset.metric);
}));
document.getElementById('download').addEventListener('click',()=>{
 const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
 const a=document.createElement('a');a.href=url;a.download='karpo-concise-results.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
let closed=[];window.addEventListener('beforeprint',()=>{closed=[...document.querySelectorAll('details:not([open])')];closed.forEach(x=>x.open=true)});
window.addEventListener('afterprint',()=>closed.forEach(x=>x.open=false));render('p50_ms');
</script></body></html>"""
    targets = {
        "RESULTS": EXP / "RESULTS.zh.md", "PLAN": EXP / "PLAN.zh.md",
        "README": ROOT / "README.md", "ERRORS": EXP / "results/quality-errors.json",
    }
    destinations = [EXP / "report.html"] if family_complete() else [ROOT / "report.html", EXP / "report.html"]
    for destination in destinations:
        page = re.sub(r"@@([A-Z]+)@@", lambda m: esc(os.path.relpath(targets[m[1]], destination.parent)), html_page)
        if destination.parent == ROOT:
            page = page.replace('<section id="overview">', '<section id="overview">' + family_notice(destination), 1)
        destination.write_text(page)
    evidence = {str(p.relative_to(EXP)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((EXP / "results").rglob("*"))
                if p.is_file() and p.name != "evidence-sha256.json"}
    (EXP / "results/evidence-sha256.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(conclusion)
    updated = "experiment" if family_complete() else "main and experiment"
    print(f"Verified {total} calls; updated {updated} Markdown/HTML; quality errors={len(mismatches)}.")


if __name__ == "__main__":
    main()
