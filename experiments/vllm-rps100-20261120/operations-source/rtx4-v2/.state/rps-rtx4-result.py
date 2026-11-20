"""Write the homogeneous RTX result only after raw-record verification has completed."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"
data = json.loads((EXP / "data.json").read_text())
run = "rtx4-fp8-v1"
status = next(r for r in data["runs"] if r["name"] == run)
assert status["status"] == "complete", status
performance = [r for r in data["performance"] if r["run"] == run]
quality = {r["scenario"]: r for r in data["quality"] if r["run"] == run}
assert set(quality) == {"risk", "sop"}
confirmed = {r["scenario"] for r in data["confirmed"] if r["run"] == run}
fleet = json.loads((EXP / "results/fleet-instances.json").read_text())
assert len(fleet) == 1 and fleet[0]["purpose"] == "homogeneous-rtx4"
host = fleet[0]
infrastructure = json.loads((EXP / "results/infrastructure-current.json").read_text())
assert infrastructure["running_instances"] == 4
total_cost = infrastructure["reference_compute_usd_per_hour"]
rtx_cost = sum(i.get("reference_compute_usd_per_hour", 0) for i in infrastructure["instances"]
               if i["state"] == "running" and i["type"].startswith("g7e."))

lines = [
    "# 四张同型号 RTX 验证结果", "",
    "用户选择的四张 RTX PRO 6000 Blackwell Server Edition 96GB 已完成独立验证。"
    + "风控" + ("通过" if "risk" in confirmed else "未通过")
    + "；SOP" + ("通过" if "sop" in confirmed else "未通过") + "。", "",
    "原 Karpo 4B、完整 200 个风控特征、完整 200 个 SOP 候选保持不变。"
    "vLLM 0.34.0，FP8 按通道量化，每张 GPU 一个完整模型副本，最大序列数 4。", "",
    "## 固定 100 RPS 测量", "",
    "| 场景 | 阶段 | 持续时间 | 有效/全部请求 | 稳态完成 RPS | 端到端 P50 | P95 | 正确/有效响应 | 性能门槛 |",
    "|---|---|---:|---:|---:|---:|---:|---:|---|",
]
for row in performance:
    assert set(row["replica_counts"]) == {"0", "1", "2", "3"}, row["replica_counts"]
    phase = {"rps100": "探测", "confirm": "正式测试", "repeat": "独立复测"}[row["stage"]]
    passed = row["passes_probe"] if row["stage"] == "rps100" else row["passes_performance_acceptance"]
    lines.append(
        f"| {'风控' if row['scenario']=='risk' else 'SOP'} | {phase} | {row['offered_seconds']:g}秒 | "
        f"{row['valid']:,}/{row['attempts']:,} | {row['steady_valid_rps']:.2f} | "
        f"{row['p50_ms']:.2f}ms | {row['p95_ms']:.2f}ms | {row['correct']:,}/{row['valid']:,} | "
        f"{'通过' if passed else '未通过'} |")
lines += [
    "", "端到端时间从预定到达时刻计算至完整 HTTP 响应，包含客户端发起滞后、"
    "HTTP、代理、跨区域网络和 SSH 转发、编码、排队与推理。"
    "表中延迟统计有效完整响应，但任何拒绝、超时或无效响应均使阶段不通过。"
    "探测失败的场景没有继续执行两次 120 秒验收。", "",
    "## 合成质量", "",
]
for scenario, q in quality.items():
    lines.append(f"- {'风控' if scenario=='risk' else 'SOP'}独立质量集："
                 f"{q['correct']}/{q['attempts']}，{q['accuracy']:.2%}，HTTP 失败 {q['failures']}。")
lines += [
    "", "独立 quality 样本集与压力测试重复使用的 tune 样本集不同，正确率分别报告。"
    "合成结果不能替代真实生产数据准确率。", "",
    "## 部署与费用", "",
    "| 副本编号 | EC2 | GPU 数量 | 位置 | 压测入口 |",
    "|---|---|---:|---|---|",
    "| 0 | g7e.4xlarge / i-0de4f061c2a04fe4b | 1 | us-east-2c | 控制节点本地 HTTP |",
    "| 1、2 | g7e.12xlarge / i-05b24e3b4a7ee864d | 2 | us-east-2c | 私网 HTTP |",
    f"| 3 | {host['InstanceType']} / {host['InstanceId']} | 1 | "
    f"{host['Placement']['AvailabilityZone']} | 控制节点本地 SSH 转发 |",
    "", "每个请求由最少在途请求代理分配给一个完整模型副本。"
    "us-east-2 Spot 配额已用满，三个可用区的单卡按需规格容量均不足，"
    "因此第四张 RTX 部署到邻近 us-east-1。"
    "使用现有私有镜像的跨区域副本；SSH 仅允许原控制节点的单个地址，推理端口不对公网开放。", "",
    f"按资源快照 {infrastructure['utc']} 的 Spot 报价，四张 RTX 计算费用约 **{rtx_cost:.2f} 美元/小时**。"
    f"原 H100 按用户要求继续运行，未进入本轮代理；保留全部实例的计算费用约 **{total_cost:.2f} 美元/小时**。"
    "不含磁盘、网络、公网 IPv4 等，Spot 报价可能变化。", "",
    "本轮没有关闭任何 EC2。测量完成后压测驱动结束，模型服务仍保持运行；"
    "进程运行时限与 EC2 生命周期分开。生产外部入口和特征查询尚未纳入本次测试。", "",
    "完整数据见[总报告](RESULTS.zh.md)、[结构化结果](data.json)、"
    "[资源快照](results/infrastructure-current.json)、"
    "[四卡原始测量](results/runs/rtx4-fp8-v1/)与[资源准备证据](results/rtx4/)。", "",
]
(EXP / "RTX4.zh.md").write_text("\n".join(lines))
print(json.dumps({"run": run, "confirmed": sorted(confirmed),
                  "performance_stages": len(performance), "rtx_compute_usd_per_hour": rtx_cost,
                  "retained_compute_usd_per_hour": total_cost}))
