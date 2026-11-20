"""Render an explicitly unmeasured experiment and its recorded capacity blocker."""
import collections
import html
import json
import os
from pathlib import Path

from report_common import table

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/family-20261120"


def load(path):
    return json.loads(path.read_text())


def notice(destination):
    if (EXP / "results/suite-status.json").exists():
        return ""
    path = EXP / "results/capacity-status.json"
    if not path.exists():
        return ""
    status = load(path)
    if status["status"] != "blocked_capacity":
        return ""
    assert status["model_calls"] == 0
    link = html.escape(os.path.relpath(EXP / "report.html", destination.parent))
    return (
        '<div class="panel panel-padding text-block" style="margin:0 0 24px;border-left:4px solid #b57621">'
        '<p><strong>2026-11-20 新实验：12B / 9B / 4B 尚未实测</strong></p>'
        '<p>测试输入和脚本已冻结，本地预检通过；G7e、B200 及 H100 的启动尝试未取得 AWS 容量。'
        '新增模型调用为 0。下方仍为 11 月 19 日已完成的 31B 描述实验。</p>'
        f'<p><a href="{link}">查看新实验方案、容量记录和实例状态</a></p></div>'
    )


def main():
    suite = EXP / "results/suite-status.json"
    if suite.exists():
        from latest_report import family_complete
        if family_complete():
            from family_report import main as render
            return render()
        raise RuntimeError("A GPU run has started; refusing to render the historical capacity blocker as current.")
    status = load(EXP / "results/capacity-status.json")
    preflight = load(EXP / "results/preflight.json")
    manifest = load(EXP / "manifest.json")
    assert status["status"] == "blocked_capacity" and status["model_calls"] == 0
    assert status["new_instances_created"] == 0 and status["all_project_gpu_instances_stopped"]
    assert preflight["status"] == "passed" and preflight["model_calls"] == 0
    rows = [
        [c["model"], c["precision"].upper(),
         "Triton Attention + Graphs" if c["model"] in ["31B", "12B"] else "Graphs",
         "104", "1 / 2 / 4 / 8" if c["concurrency"] else "串行对照", "尚未执行"]
        for c in manifest["configs"]
    ]
    counts = collections.Counter((a["region"], a["type"], a["market"], a["error_code"])
                                 for a in status["attempts"])
    capacity_rows = [[region, kind, "Spot" if market == "spot" else "按需", n,
                      "容量不足" if code == "InsufficientInstanceCapacity" else code]
                     for (region, kind, market, code), n in sorted(counts.items())]
    conclusion = (
        "本轮尚未进行 GPU 实测：新增模型调用为 0。已经冻结 Karpo 31B、12B、9B、4B 的权重版本、"
        "完整 200 候选的英文精简输入及 7 个测试配置，并通过本地预检。"
        "AWS 启动与恢复尝试未取得可用容量，因此目前没有 12B / 9B / 4B 或 B200 的新正确率和延迟。"
    )
    method = (
        "沿用之前的 40 个原有合成案例及 12 个边界案例，每例两种候选顺序，共 104 次质量观察。"
        "所有候选一次传入模型，程序不筛选候选。FP8 配置额外测试并发 1/2/4/8，"
        "BF16 用来对照量化影响。计划 1168 次调用包含预热和重复请求，实际执行 0 次。"
        "不同模型的 token 长度、运行兼容性、正确率及性能仍待 GPU 验证。"
    )
    resource_text = (
        "资源核对时，新建实例为 0；原 GPU 实例 i-048c3784bb87b6d86 仍为 stopped，EBS 保留。"
        "未遗留本轮处于 open / active 状态的匹配 Spot 请求，也未安排后台自动重试。"
        "后续恢复测试仍遵循只 stop、不 terminate。"
    )
    tables = [
        ("已冻结的 G7e 测试配置", ["模型", "精度", "推理路径", "计划质量观察", "计划并发", "执行状态"], rows),
        ("实际容量尝试", ["区域", "实例", "方式", "尝试次数", "AWS 结果"], capacity_rows),
    ]
    markdown = ["# Karpo 小模型测试：等待 GPU 容量", "", conclusion, "", method, ""]
    for title, headers, cells in tables:
        markdown += ["## " + title, "", "| " + " | ".join(headers) + " |",
                     "|" + "|".join("---" for _ in headers) + "|"]
        markdown += ["| " + " | ".join(map(str, row)) + " |" for row in cells]
        markdown.append("")
    markdown += [
        "B200 已确认有对应机型、配额和 Spot 支持，但启动失败；H100 作为替代硬件的尝试也未成功。"
        "机型在售或配额充足不等于有即时容量。H100 其中一次报价条件错误调整后，仍返回容量不足。", "",
        resource_text, "", "资源观察时间：" + status["observed_utc"], "",
        "冻结计划：[PLAN.zh.md](PLAN.zh.md)。冻结配置：[manifest.json](manifest.json)。",
        "本地预检：[preflight.json](results/preflight.json)。"
        "实际容量和资源记录：[capacity-status.json](results/capacity-status.json)。", "",
        "上一次完成的实测：[31B 描述实验](../concise-fp8-20261119/report.html)。", "",
        "在已准备好的 G7e GPU 环境中启动本实验：", "",
        "```bash", "python scripts/family_preflight.py",
        "venv/bin/python scripts/family_suite.py --bucket <results-bucket>", "```", "",
        "执行脚本需要项目根目录的 venv 和固定版本的 Karpo / PyTorch 依赖；"
        "GPU 作业退出后由启动器停止实例。运行结果保存在实验目录，并持续同步至指定 S3 bucket。", "",
    ]
    (EXP / "STATUS.zh.md").write_text("\n".join(markdown))
    esc = html.escape
    sections = "".join(
        f'<section id="part-{i}"><div class="section-head"><h2>{esc(title)}</h2></div>'
        f'<div class="panel">{table(headers, cells, title)}</div></section>'
        for i, (title, headers, cells) in enumerate(tables))
    data = json.dumps({"status": status, "preflight": preflight, "manifest": manifest},
                      ensure_ascii=False).replace("<", "\\u003c")
    css = (ROOT / "scripts/report.css").read_text()
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Karpo 小模型测试 · 尚未执行</title>
<style>{css}</style></head><body>
<aside class="sidebar"><a class="brand" href="#overview"><span class="brand-mark">K</span> Karpo Decision</a>
<div class="side-label">MODEL FAMILY / EXPERIMENT STATUS</div><nav class="nav">
<a href="#overview">实际进度</a><a href="#part-0">冻结配置</a><a href="#part-1">容量记录</a>
<a href="#resources">资源状态</a><a href="../concise-fp8-20261119/report.html">上次实测</a></nav></aside>
<main><div class="topbar"><div class="crumb">2026-11-20 / 新实验进度</div>
<div class="top-actions"><button class="button" id="download">下载状态记录</button>
<button class="button primary" onclick="window.print()">打印 / PDF</button></div></div>
<section id="overview"><div class="eyebrow">FULL 200 CANDIDATES · NOT YET MEASURED</div>
<h1 style="font-size:clamp(26px,3vw,36px);line-height:1.4;margin:16px 0">测试准备完成，<br>等待 GPU 容量</h1>
<p class="text-block">{esc(conclusion)}</p>
<div class="pills"><span class="pill">实际模型调用：0</span><span class="pill">7 个配置已冻结</span>
<span class="pill">本地预检通过</span><span class="pill">无新增运行实例</span></div>
<div class="panel panel-padding text-block"><p>{esc(method)}</p>
<p>页面不提供新模型的性能图或正确率数值，因为 GPU 测试尚未执行。</p></div></section>
{sections}<section id="resources"><div class="section-head"><h2>实例与证据</h2></div>
<div class="panel panel-padding text-block"><p>{esc(resource_text)}</p>
<p>观察时间：{esc(status['observed_utc'])}</p>
<p><a href="results/preflight.json">本地预检</a> · <a href="results/capacity-status.json">容量及资源记录</a>
 · <a href="manifest.json">冻结配置</a></p></div></section>
<footer class="footer"><span>仅记录实际进度；不推算未执行的结果</span><div class="footer-links">
<a href="STATUS.zh.md">Markdown</a><a href="PLAN.zh.md">实验计划</a><a href="../../report.html">主报告</a></div></footer>
</main><script type="application/json" id="data">{data}</script>
<script>const data=JSON.parse(document.getElementById('data').textContent);
document.getElementById('download').addEventListener('click',()=>{{
const u=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{{type:'application/json'}}));
const a=document.createElement('a');a.href=u;a.download='karpo-family-status.json';a.click();
setTimeout(()=>URL.revokeObjectURL(u),1000);}});</script></body></html>"""
    (EXP / "report.html").write_text(page)
    print("Rendered unmeasured family experiment status; no new performance results.")


if __name__ == "__main__":
    main()
