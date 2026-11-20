# Karpo 小模型测试：等待 GPU 容量

本轮尚未进行 GPU 实测：新增模型调用为 0。已经冻结 Karpo 31B、12B、9B、4B 的权重版本、完整 200 候选的英文精简输入及 7 个测试配置，并通过本地预检。AWS 启动与恢复尝试未取得可用容量，因此目前没有 12B / 9B / 4B 或 B200 的新正确率和延迟。

沿用之前的 40 个原有合成案例及 12 个边界案例，每例两种候选顺序，共 104 次质量观察。所有候选一次传入模型，程序不筛选候选。FP8 配置额外测试并发 1/2/4/8，BF16 用来对照量化影响。计划 1168 次调用包含预热和重复请求，实际执行 0 次。不同模型的 token 长度、运行兼容性、正确率及性能仍待 GPU 验证。

## 已冻结的 G7e 测试配置

| 模型 | 精度 | 推理路径 | 计划质量观察 | 计划并发 | 执行状态 |
|---|---|---|---|---|---|
| 31B | FP8 | Triton Attention + Graphs | 104 | 1 / 2 / 4 / 8 | 尚未执行 |
| 12B | FP8 | Triton Attention + Graphs | 104 | 1 / 2 / 4 / 8 | 尚未执行 |
| 12B | BF16 | Triton Attention + Graphs | 104 | 串行对照 | 尚未执行 |
| 9B | FP8 | Graphs | 104 | 1 / 2 / 4 / 8 | 尚未执行 |
| 9B | BF16 | Graphs | 104 | 串行对照 | 尚未执行 |
| 4B | FP8 | Graphs | 104 | 1 / 2 / 4 / 8 | 尚未执行 |
| 4B | BF16 | Graphs | 104 | 串行对照 | 尚未执行 |

## 实际容量尝试

| 区域 | 实例 | 方式 | 尝试次数 | AWS 结果 |
|---|---|---|---|---|
| us-east-1 | g7e.8xlarge | 按需 | 3 | 容量不足 |
| us-east-1 | g7e.8xlarge | Spot | 3 | 容量不足 |
| us-east-1 | p6-b200.48xlarge | 按需 | 1 | 容量不足 |
| us-east-1 | p6-b200.48xlarge | Spot | 1 | 容量不足 |
| us-east-2 | g7e.4xlarge | Spot | 3 | 容量不足 |
| us-east-2 | g7e.8xlarge | 按需 | 3 | 容量不足 |
| us-east-2 | g7e.8xlarge | Spot | 4 | 容量不足 |
| us-east-2 | p5.4xlarge | 按需 | 3 | 容量不足 |
| us-east-2 | p5.4xlarge | Spot | 4 | 容量不足 |
| us-east-2 | p5.4xlarge | Spot | 1 | SpotMaxPriceTooLow |
| us-east-2 | p6-b200.48xlarge | 按需 | 3 | 容量不足 |
| us-east-2 | p6-b200.48xlarge | Spot | 3 | 容量不足 |
| us-west-2 | g7e.8xlarge | Spot | 4 | 容量不足 |
| us-west-2 | p6-b200.48xlarge | Spot | 3 | 容量不足 |

B200 已确认有对应机型、配额和 Spot 支持，但启动失败；H100 作为替代硬件的尝试也未成功。机型在售或配额充足不等于有即时容量。H100 其中一次报价条件错误调整后，仍返回容量不足。

资源核对时，新建实例为 0；原 GPU 实例 i-048c3784bb87b6d86 仍为 stopped，EBS 保留。未遗留本轮处于 open / active 状态的匹配 Spot 请求，也未安排后台自动重试。后续恢复测试仍遵循只 stop、不 terminate。

资源观察时间：2026-11-20T02:59:38.604066+00:00

冻结计划：[PLAN.zh.md](PLAN.zh.md)。冻结配置：[manifest.json](manifest.json)。
本地预检：[preflight.json](results/preflight.json)。实际容量和资源记录：[capacity-status.json](results/capacity-status.json)。

上一次完成的实测：[31B 描述实验](../concise-fp8-20261119/report.html)。

在已准备好的 G7e GPU 环境中启动本实验：

```bash
python scripts/family_preflight.py
venv/bin/python scripts/family_suite.py --bucket <results-bucket>
```

执行脚本需要项目根目录的 venv 和固定版本的 Karpo / PyTorch 依赖；GPU 作业退出后由启动器停止实例。运行结果保存在实验目录，并持续同步至指定 S3 bucket。
