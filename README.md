# Karpo Decision Benchmark

独立的 Karpo 决策模型测试项目。目标是验证约 200 个候选下的选择质量、请求延迟与并发吞吐，不执行任何真实金融操作。

测试对象为 `MachinePulseAI/karpo-decision-31B`。首轮三组配置测试均已完成，共记录 1111 次调用（含预热、重复性能测试）。代码与权重版本已冻结，输入在模型调用前生成并保存哈希。首轮结论见 [RESULTS.zh.md](RESULTS.zh.md)；原始证据在 `results/`。

浏览器版见 [report.html](report.html)：单文件离线报告，支持切换 P50/P95、候选数量与描述长度对比、并发数据查看、下载汇总 JSON，以及打印为 PDF。实例状态带采集时间，不会实时查询 AWS。

修改汇总或资源记录后，可运行 `python scripts/build_html.py` 重新生成 HTML（仅用 Python 标准库）。生成器会核对三组原始调用记录与冻结验证记录的哈希；不会启动 GPU 或修改云资源。

单张 RTX PRO 6000 96GB 上，最快的 FP8 配置处理 200 个候选长描述时 P50 1350.1 ms、P95 1362.3 ms，短描述时 P50 839.7 ms，未达到 100 ms 目标。完整原始记录已通过 `scripts/verify_results.py` 核验。

**最新实验：完整 200 候选 + 精简描述 + FP8**

固定全部 200 个候选，由模型一次选择；不使用程序业务域判断、召回或候选过滤。同一 FP8 + Triton / Graphs 服务比较四种描述，共 848 次调用。报告见 [HTML](experiments/concise-fp8-20261119/report.html)、[实测结论](experiments/concise-fp8-20261119/RESULTS.zh.md)及[冻结计划](experiments/concise-fp8-20261119/PLAN.zh.md)。历史报告中关于先筛候选的建议不作为当前方案。

| 描述 | 原有场景 P50 | 原有观察正确 | 新增边界正确 |
|---|---:|---:|---:|
| 原始长描述 | 1346.8 ms | 80/80 | 22/24 |
| 英文精简，保留条件 | 705.4 ms | 80/80 | 22/24 |
| 中文精简，保留条件 | 827.6 ms | 80/80 | 23/24 |
| 公共规则 + 候选引用 | 554.7 ms | 78/80 | 20/24 |

英文精简适合作为继续优化的起点，本批观察的选择与基线一致，P50 降低约 48%；公共规则去重更快，但新增错误。四组均未达到 100 ms。原有 40 例、新增 12 例各测两种候选顺序，均为合成场景，不能据此证明生产准确率或 SOTA。

本轮使用支持停机的 persistent Spot，并将约 62.6GB 原始权重保存在 EBS。已确认实例为 **stopped**，保留实例和磁盘；最新带时间的状态见 [本轮资源记录](experiments/concise-fp8-20261119/results/infrastructure.json)。下方 AWS 历史记录描述的是首轮实例。

`scripts/concise_report.py` 从冻结输入和逐条响应重新核验 848 次调用后生成最新报告；它不调用 GPU。四组输入、运行代码和原始结果均独立保存在 `experiments/concise-fp8-20261119/` 及 `scripts/concise_*.py`。

**测试数据**

- 200 个候选 SOP：10 个业务域 × 20 类处理动作。
- 40 个合成业务快照，来自 20 类规则模板；每例测试原始与反转候选顺序。
- 20 / 50 / 100 / 200 候选，以及短描述 / 带适用条件的长描述。
- 从已有 StrandsDecider 项目冻结复制的 40 个支付快照、两种候选顺序，共 80 个配对观察，每次 2–6 个候选。
- 合成数据不代表 Redotpay 的真实政策和分布；候选换序、重复请求不是新增独立案例，也不能用本测试宣布通用 SOTA。

20 / 50 / 100 项的子集在构造时保留正确答案及同业务域候选，用于研究输入规模的影响。它们不是实际检索器的输出，不能据此证明“先召回再决策”的端到端召回率。

**计时口径**

客户端与模型运行在同一实例，以 loopback HTTP 发起请求；延迟包含发送、服务端预处理、排队、推理与读取完整响应，未包含跨区域或生产入口网络。

预热单列，不计入正式指标。不检索缩减 200 个候选。HTTP / 格式失败计入完成率分母，不重试覆盖。P50/P95/P99 只统计有效响应，同时报告失败数、正确率与 100 ms 内正确完成率。

除固定 worker 的并发扫描外，还包含按固定到达率发送的负载。后者延迟从计划发送时间起算，客户端限流丢弃也计失败；避免慢响应降低发送速率而隐藏排队。

官方 `Server-Timing` 的 `model` 字段包含 Runner 的锁等待，不能作为纯 GPU kernel 耗时。

**本地检查**

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

`fixtures/` 和复制的支付源文件使测试集可独立使用。`scripts/prepare.py` 是本次生成器；只有重新从原项目导入支付源时，才依赖原项目路径。

**运行**

在有 Python 3.12、NVIDIA 驱动及兼容 CUDA 的 GPU 主机上：

```bash
pip install --extra-index-url https://download.pytorch.org/whl/cu130 \
  "karpo-decision @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de" \
  "torch==2.14.1+cu130" "torchvision==0.29.1+cu130" \
  "transformers==5.21.0" "triton==3.8.0" "accelerate==1.15.0"
```

使用权重 revision `b8e6fd7463cea8662c556d6257955d7ef9c10d19`。本项目保留上游的 `sources/requirements-serve.txt`，实际解析的安装版本记录于 `results/pip-freeze.txt`。

```bash
HF_HUB_DISABLE_XET=1 hf download MachinePulseAI/karpo-decision-31B \
  --revision b8e6fd7463cea8662c556d6257955d7ef9c10d19 \
  --local-dir models/karpo-31b --exclude 'assets/*'

torchrun --standalone --nproc_per_node 1 \
  -m karpo_decision.serve.server \
  --checkpoint models/karpo-31b --max-length 32768 --port 18440

# 在另一个终端运行；服务保持运行。
python scripts/benchmark.py --name unique-run-name --phase all
```

`--phase smoke` 只做预热和协议检查；正式测试可分为 `quality`、`performance`。每次运行使用新的名称，脚本拒绝覆盖已有结果。

`scripts/run_suite.py` 用于本次 EC2 的 `/opt/karpo` 布局，顺序运行 BF16、BF16 + Triton/Graphs、FP8 + Triton/Graphs，并每 15 秒向私有 S3 同步结果。不同路径必须同时比较答案和耗时；FP8 不假定无损。

每组结束后脚本主动发送 SIGTERM 释放 GPU；因此服务日志末尾可能有 torchrun 的 `SignalException ... signal: 15`。运行完成状态由 `complete.json` / `suite-outcomes.json` 判定，请求成功与否以逐条响应记录为准。

上游 Gemma CUDA Graphs 只覆盖单问题、纯文本、最长 8192 tokens 的请求；超过此长度会自动走非 Graphs 路径。配置名表示启动选项，并不保证每个请求都使用 Graphs。图捕获在模型启动时完成，另外记录，不混入预热后延迟。

RTX PRO 6000 的计算能力为 sm_120，上游专门提供 `--triton-attention`。该版本 `blackwell` 可选依赖中的 FROST / FA4 路径只允许 sm_100，不能因为显卡名称都包含 Blackwell 就直接认为可用；相关能力判断源码保存在 `sources/src/karpo_decision/families/gemma/`。

**AWS 资源**

`scripts/cloud.py` 管理本项目的临时实例、SSM 执行与私有 S3 文件；资源清单位于不进入 Git 的 `.state/cloud.json`。模型仅监听 loopback，无公网入站端口。

本次先尝试单卡 H100 / RTX PRO 6000 的按需容量，再尝试 Spot。Spot 可能被回收；已上传结果保留在 S3。启动脚本设置 120 分钟无人值守关机保护，这是上限保护，不是计划测试时长。EC2 对关机的处理取决于实例配置；本次使用的一次性 Spot 不支持保留实例停机。

实际获得 `us-east-2c` 的 `g7e.8xlarge` Spot：32 vCPU、256 GiB 内存、单张 RTX PRO 6000 Blackwell 96GB。

上游 `--fp8` 会先将原权重加载到 GPU，再量化 decoder projections。量化后的显存占用不等于启动所需显存，不能仅凭运行时占用低于 48GB 就认为原启动流程可在 48GB GPU 上运行。

用户最新要求：**测试结束后只 stop GPU，不 terminate**。`python scripts/cloud.py stop` 现在只调用 `StopInstances`；不支持停机的实例会报错，绝不自动转成终止。`cleanup.py` 的删除操作在当前资源记录下已禁用，`status` 仍可查询。

未来的 `try_spot.py` 已改为 persistent Spot + interruption behavior `stop`，启动脚本将权重放在 EBS，支持保留环境停机。重新启动仍取决于 Spot 容量。本次历史运行配置仍为 one-time Spot，不能通过修改本地脚本改变已提交的实例。

本次 GPU 实例的终止请求于 2026-11-19 12:41:58 UTC 提交，早于用户的上述消息；AWS 无法撤回该请求。随后停止了其他资源清理，成功关闭系统盘 `vol-0f216baca898aebbe` 的随实例删除选项，并提交快照 `snap-0a6e91176fce5e8fb`。快照完成状态及实例终态见 [云资源记录](results/cloud-lifecycle.json)。本地结果、S3、专用 IAM 和安全组均保留。历史运行的权重位于实例 NVMe，不包含在系统盘快照中，可按冻结 revision 重新下载。
