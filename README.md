# Karpo Decision Benchmark

独立的 Karpo 工具 / SOP 选择测试项目。每次完整传入 200 个候选，由模型一次选择；程序不做业务域判断或候选过滤。

**最新结果：2026-11-20**

在 RTX PRO 6000 Blackwell 上完成 31B / 12B / 9B / 4B 的七组单卡配置，以及官方单副本 / 双副本对照，共核验 **1,712 次调用**。这批测试中，最快的 4B FP8 为 123.3 ms、98/104 参考一致；质量最好的 12B BF16 为 432.6 ms、104/104。这里延迟均为 P50，没有配置达到 100 ms。

主报告：[report.html](report.html)。支持 P50/P95 切换、质量与换序稳定性、FP8/BF16 对照、并发、显存与启动记录、下载汇总 JSON，以及打印 PDF。

文字报告：[RESULTS.zh.md](RESULTS.zh.md)。所有数值由逐条响应重算；实例状态是带时间的记录。

| 配置 | P50 | P95 | 参考一致 | 100ms 内正确 |
|---|---:|---:|---:|---:|
| 31B FP8 | 706.3 ms | 708.6 ms | 102/104 | 0/104 |
| 12B FP8 | 277.7 ms | 278.9 ms | 103/104 | 0/104 |
| 12B BF16 | 432.6 ms | 433.8 ms | 104/104 | 0/104 |
| 9B FP8 | 176.8 ms | 177.3 ms | 83/104 | 0/104 |
| 9B BF16 | 263.0 ms | 263.6 ms | 82/104 | 0/104 |
| 4B FP8 | 123.3 ms | 124.1 ms | 98/104 | 0/104 |
| 4B BF16 | 169.6 ms | 169.8 ms | 98/104 | 0/104 |

每行是 52 个合成案例的两种候选顺序，共 104 次质量观察，不能代表 Redotpay 生产准确率或 SOTA。9B / 4B 的模型大小与本批质量不呈单调关系，具体错误和顺序变化见 HTML 及 [逐条错误](experiments/family-20261120/results/quality-errors.json)。

**副本与高并发**

副本测试按预先冻结的规则，在 FP8 配置中选择没有新增 31B 基线错误的最快模型，实际选中 `31b-fp8`。每张 GPU 加载一份完整模型，单次请求由一张卡处理。该规则与全配置质量对照分别报告。

| GPU 副本 | 并发8有效请求/秒 | 并发8 P95 | 质量参考一致 |
|---|---:|---:|---:|
| 1 | 1.442 | 5533.2 ms | 102/104 |
| 2 | 2.850 | 2813.8 ms | 102/104 |

并发扫描包含重复请求，属于短时吞吐和排队观察，不能作为持续高负载 SLA。增加副本不会直接将单次模型计算时间按卡数等比分摊。

**输入与计时**

候选描述采用上一轮的英文精简版本，状态保留原有中英文；候选、条件、参考答案及顺序均已冻结。40 个原有案例、12 个边界案例，每例测原序和反序。程序没有业务域预筛选，输入不截断。

客户端与模型同机，通过 loopback HTTP 计时到完整响应，包含预处理与排队，未包含生产入口网络或工具执行。预热单列，错误计入尝试分母。系列实验的 FP8 配置扫描并发 1/2/4/8，每格24次；副本实验扫描 1/2/4/8/16，每格32次。各模型使用自己的 tokenizer。

**GPU 与部署**

实际实例为 `g7e.12xlarge` Spot：48 vCPU、512 GiB内存、两张 RTX PRO 6000 Blackwell 96GB。系列模型每次使用一张 GPU，副本对照分别使用一张和两张。原 `g7e.8xlarge` 实例保留。

部署使用 Karpo 官方 HTTP 服务（`karpo_decision.serve.server`），由 `torch.distributed.run` 启动。PyTorch `2.14.1+cu130`、Transformers `5.21.0`；代码 revision 为 `6600011eb5cf46232d056ec0717dc726146d29de`。Gemma 模型使用 Triton Attention + Graphs，Qwen 模型使用 Graphs；FP8/BF16 分开对照。具体参数和 health 响应保存在每个配置的结果目录。

首轮镜像缺少 Qwen 依赖，9B/4B 四个配置在启动阶段失败，模型调用为0。补装 `flash-linear-attention==0.5.2`、`fla-core==0.5.2`、`einops==0.8.2` 后，在同一实例重启后单独重试；模型与核心运行库版本保持不变。首轮成功的31B/12B数据及失败记录全部保留。

另外尝试了 B200 和 H100。最新 H100 尝试覆盖 us-east-1 / us-west-2 共10个可用区，Spot及按需20次均缺少容量，没有新增 H100 实例或测量结果。见 [H100记录](experiments/h100-4b-20261120/STATUS.zh.md)。

**实验与复核**

- [系列实验](experiments/family-20261120/PLAN.zh.md)：首轮三个成功配置共528次调用；合并核验见 [verification.json](experiments/family-20261120/results/verification.json)。
- [Qwen依赖修复后重试](experiments/qwen-retry-20261120/PLAN.zh.md)：四个配置共640次，输入逐字相同。
- [单副本与双副本](experiments/replicas-retry-20261120/PLAN.zh.md)：544次；原始选择规则和首次退出记录也保留。
- [上一轮描述实验](experiments/concise-fp8-20261119/report.html)：四种描述共848次，属于历史结果。历史表的延迟使用80次原有观察，新系列表使用原有及边界共104次，口径分别标明。

重新生成主 HTML 和 Markdown：

```bash
python -m pip install -e .
python scripts/build_html.py
```

`scripts/report.py` 使用同一入口。生成器校验冻结文件、全部响应、覆盖范围、汇总数值和停止状态，不启动 GPU。运行新测试时应创建新的实验目录并冻结配置；现有执行文件及输入不可覆盖。

**实例保留**

测试结束后已确认新旧 GPU 实例均为 **stopped**，没有 terminate。实例及 EBS 上的模型保留；Spot中断行为、实例关机行为均为stop，系统盘随实例删除为false。最近核对时间：2026-11-20T04:14:14.567758+00:00。

[资源状态](experiments/family-20261120/results/infrastructure.json) · [Qwen结束检查](experiments/qwen-retry-20261120/results/postflight.json) · [副本结束检查](experiments/replicas-retry-20261120/results/postflight.json)
