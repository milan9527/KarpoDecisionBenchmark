# Karpo Decision Benchmark

独立的 Karpo 决策模型测试项目。当前业务要求：每次完整传入 200 个工具 / SOP 候选，由模型一次选择；程序不做业务域判断、召回或候选过滤，不执行真实金融操作。

测试对象为 `MachinePulseAI/karpo-decision-31B`。当前报告使用同一个 FP8 + Triton / Graphs 服务的四组描述实验，共记录并核验 848 次调用。

**查看结果**

主报告：[report.html](report.html)。支持四组描述的 P50/P95 对比、正确率与错误明细、候选换序稳定性、并发数据、下载汇总 JSON，以及打印为 PDF。页面与下载数据均使用本轮完整 200 候选实验。

文字报告：[RESULTS.zh.md](RESULTS.zh.md)。实验计划及逐条记录位于 [experiments/concise-fp8-20261119](experiments/concise-fp8-20261119/)。实例状态是带时间的记录，不实时查询 AWS。

| 描述 | 原有场景 P50 | 原有观察正确 | 新增边界正确 |
|---|---:|---:|---:|
| 原始长描述 | 1346.8 ms | 80/80 | 22/24 |
| 英文精简，保留条件 | 705.4 ms | 80/80 | 22/24 |
| 中文精简，保留条件 | 827.6 ms | 80/80 | 23/24 |
| 公共规则 + 候选引用 | 554.7 ms | 78/80 | 20/24 |

英文精简适合作为继续优化的起点，本批观察的选择与基线一致，P50 降低约 48%；公共规则去重更快，但新增错误。四组均未达到 100 ms。英文精简并发 8 时吞吐约 1.43 请求/秒、P95 约 5.59 秒，官方服务仍有明显排队。

**测试数据与计时**

- 每次 200 个候选 SOP：10 个业务域 × 20 类处理动作。
- 原有 40 个合成快照，新增 12 个优先级、边界及干扰用例；每例测试原序及反序。
- 每组 104 次质量观察；四组共 416 次质量观察，另有 32 次预热、384 次并发观察及 16 次结束基线复测。
- 各组保留相同状态、候选 ID、顺序及参考答案，只改变描述表达。

配对观察和重复请求不是新增独立案例，合成规则不能代表 Redotpay 真实政策或生产准确率，也不能证明 SOTA。公共规则去重利用了本目录的重复规则结构，不能推断 200 条完全不同的 SOP 也能同比压缩。

客户端与模型运行在同一实例，以 loopback HTTP 发起请求；延迟包含预处理、排队、推理及读取完整响应，未包含生产入口网络和工具执行。预热单列，错误计入尝试分母。并发 1/2/4/8 每格 24 次，属于短时扫描。

官方 `Server-Timing` 的 `model` 字段包含 Runner 锁等待，不是纯 GPU kernel 耗时。增加 HTTP worker 不能直接视为推理吞吐提升。

**重新生成报告**

```bash
python -m pip install -e .
python scripts/build_html.py
```

生成器先核验冻结输入、逐条响应、质量覆盖和汇总指标，再同时更新主目录与实验目录的 HTML / Markdown。该操作不启动 GPU 或修改云资源。`scripts/report.py` 和 `scripts/concise_report.py` 使用同一个生成入口。

输入生成及测试执行脚本分别为 `scripts/concise_prepare.py`、`scripts/concise_benchmark.py` 和 `scripts/concise_suite.py`。脚本拒绝覆盖已有实验；进行新一轮测试时应创建独立的实验目录并重新冻结输入。

**模型与推理环境**

本轮为 AWS `us-east-2c` 的 `g7e.8xlarge`：32 vCPU、256 GiB 内存，单张 RTX PRO 6000 Blackwell 96GB。

上游代码 revision：`6600011eb5cf46232d056ec0717dc726146d29de`。权重 revision：`b8e6fd7463cea8662c556d6257955d7ef9c10d19`。实际运行环境与启动参数见 [bootstrap.json](experiments/concise-fp8-20261119/results/bootstrap.json) 和 [server-command.json](experiments/concise-fp8-20261119/results/server-command.json)。

上游 CUDA Graphs 仅覆盖单问题、纯文本、最长 8192 tokens 的请求。原始长描述超过此长度，自动回退至非 Graphs 路径；本轮衡量精简描述在同一启动配置下的整体效果，不能分别归因于 token 数减少或 Graphs。

FP8 路径先加载原始权重再量化部分 decoder projections，运行时显存占用不等于启动所需显存。RTX PRO 6000 的计算能力为 sm_120，本轮使用上游 Triton 路径；该版本 FROST / FA4 仅允许 sm_100。

**实例保留**

本轮使用 persistent Spot，关机和回收行为均为 stop，系统盘随实例删除为 false。GPU 实例已确认 **stopped**，实例及 EBS 保留，约 62.6GB 原始权重位于根 EBS。

用户要求：**测试结束后只 stop，不 terminate**。当前资源状态见 [infrastructure.json](experiments/concise-fp8-20261119/results/infrastructure.json)，权重位置和模型进程退出检查见 [postflight.json](experiments/concise-fp8-20261119/results/postflight.json)。
