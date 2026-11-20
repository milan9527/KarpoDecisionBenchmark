# H100 容量尝试未成功

2026-11-20，在 us-east-1 的 6 个可用区、us-west-2 的 4 个可用区分别尝试 `p5.4xlarge` 的 Spot 和按需实例，共 20 次启动请求，均返回 `InsufficientInstanceCapacity`。

没有创建 H100 实例，模型决策调用为 0；资源核对未发现匹配本次实验的实例或仍然 open / active 的 Spot 请求。因此没有 H100 性能或正确率结果。

本次已冻结同一套完整 200 候选、4B FP8 + Graphs 的 208 次调用计划，用于后续有容量时执行。该计划尚未执行，不应与 RTX PRO 6000 的已完成数据混为一谈。

[容量与资源核对](results/capacity-status.json) · [实验计划](PLAN.zh.md) · [冻结配置](manifest.json)
