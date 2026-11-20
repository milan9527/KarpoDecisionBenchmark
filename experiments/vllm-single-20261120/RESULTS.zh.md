# Karpo 4B：vLLM 与原生 FP8 单卡对照

使用同一张 RTX PRO 6000 Blackwell Server Edition 96GB（AWS g7e.4xlarge），两套服务顺序运行。原始输入、完整选项、Karpo 模板及 marker 后的 9 个模板 token 全部保留。

验收口径：持续 100 个在途请求，完整 HTTP P50 ≤500ms；风控与 SOP 分开。下表使用各自独立复测结果，吞吐分母包含最终排空。

| 场景 | 原生 P50 | vLLM P50 | P50 加速比 | 原生 RPS | vLLM RPS | vLLM 最大序列数 | 达标 |
|---|---:|---:|---:|---:|---:|---:|---|
| 风控 | 4233.9ms | 3382.0ms | 1.25× | 23.60 | 28.54 | 8 | 否 |
| SOP | 11983.3ms | 9234.9ms | 1.30× | 8.33 | 10.38 | 8 | 否 |

## 质量与一致性

| 场景 | 原生合成正确率 | vLLM 合成正确率 | 同答案 | 新增错误 | 最大概率差 |
|---|---:|---:|---:|---:|---:|
| 风控 | 233/240 (97.08%) | 233/240 (97.08%) | 240/240 | 0 | 0.154882 |
| SOP | 98/104 (94.23%) | 98/104 (94.23%) | 104/104 | 0 | 0.117524 |

合成正确率只反映已有策略样例的一致性，不代表生产准确率。选择配置只使用性能和预先约定的 >90% 质量门槛。

## 全部配置

| 场景 | 配置 | C1 P50 | C100 P50 | C100 P95 | RPS | 合成正确率 |
|---|---|---:|---:|---:|---:|---:|
| 风控 | native-initial | 85.9ms | 4272.0ms | 4313.0ms | 23.38 | 97.08% |
| 风控 | vllm-b1 | 44.2ms | 3657.4ms | 3662.6ms | 27.30 | 97.08% |
| 风控 | vllm-b4 | 44.8ms | 3470.1ms | 3488.2ms | 28.77 | 97.50% |
| 风控 | vllm-b8 | 44.9ms | 3383.2ms | 3657.7ms | 28.48 | 97.08% |
| SOP | native-initial | 162.8ms | 12103.8ms | 12128.0ms | 8.26 | 94.23% |
| SOP | vllm-b1 | 111.2ms | 10000.5ms | 10003.7ms | 9.99 | 94.23% |
| SOP | vllm-b4 | 111.7ms | 9648.5ms | 9659.4ms | 10.36 | 94.23% |
| SOP | vllm-b8 | 112.0ms | 9239.8ms | 10000.3ms | 10.39 | 94.23% |

## 实现与边界

- 专用 vLLM 插件加载原始 Qwen3.5 权重及 readout；全注意力双向、GatedDeltaNet 保持因果。按 Karpo marker 位置读取，保留完整序列；输出头以 FP32 运行，其权重哈希逐次启动核对。
- vLLM 0.34.0 使用逐通道 FP8 权重和逐 token 动态激活；小型门控层不量化。原生 FP8 的 kernel 与舍入不同，因此报告逐例答案和概率差异。
- 关闭前缀缓存、chunked prefill；max_model_len=8192；token budget 上限 65536。按输入长度配置 prefill CUDA Graph，两套服务都计入编码和调度开销。
- 原生 Torch=2.14.1+cu130；vLLM Torch=2.14.0+cu130；Transformers=5.21.0。比较完整服务方案，不能将全部收益归因于 GPU kernel。
- 延迟不含生产网络入口及特征查询；P95/P99 仅观察。max_num_seqs 是 GPU 调度配置，HTTP 在途并发独立固定为 100。
- vLLM 的显存占用包含 gpu_memory_utilization=0.8 的缓存预留；本次未测试最低显存配置。
- 仅支持本实验单问题纯文本请求；其他输入明确拒绝。保留既有模型、EBS 和原生环境。

## 证据

- 16,197 次 HTTP 请求全部有效；12 次持续 C100 测试；464 条编码全部一致。
- 原始日志：results/measurements；逐例答案差异：各配置 quality/parity-vs-reference.json。
- GPU 和软件证据：results/gpu.csv、environment-*.json、servers/*/engine-config.json。
- 复现配置：manifest.json、PLAN.zh.md；开发启动日志单独保留，不计入性能。
- 最终云资源状态：results/infrastructure-final.json。
