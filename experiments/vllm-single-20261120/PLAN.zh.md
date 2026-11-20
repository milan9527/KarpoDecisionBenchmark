# Karpo 4B：vLLM 适配与单卡 FP8 对照

用户授权：优先完成 vLLM 适配和单卡对照。风控、SOP 分别验证；保留原始输入，持续 100 个在途请求，完整 HTTP P50 ≤500ms，合成正确率 >90%。

## 固定条件

- 复用现有 `i-0de4f061c2a04fe4b`，AWS g7e.4xlarge，一张 RTX PRO 6000 Blackwell Server Edition 96GB。其他 GPU 实例保持停止。
- 同一台主机上顺序运行原生 Karpo FP8 服务和 vLLM FP8；不同时占用 GPU。只在本机回环地址提供测试接口。
- 固定模型 `MachinePulseAI/karpo-decision-4B`，revision `f7f521cfc3219ca28518c6bba91960b8757dce06`；Karpo revision `6600011eb5cf46232d056ec0717dc726146d29de`。
- 从 `experiments/batch100-20261120` 原样复制请求文件，逐文件验证 SHA256。保持全部字段、完整选项、选项顺序和 Karpo 原始模板，不截断，不使用答案缓存。
- vLLM 优先使用发布版 0.34.0，安装在独立虚拟环境；保留既有原生环境和模型文件。记录依赖和运行时配置差异，FP8 两种实现不宣称数值完全相同。

## 适配与验证

- 使用固定版本 Karpo Encoder，提交完整 token ID 序列。逐例核对 token、marker 位置与原生编码一致。
- 使用 Qwen3.5 双向全注意力、原有因果 GatedDeltaNet；禁止普通因果前缀缓存和 chunked prefill。
- 加载原始 readout.safetensors，以 FP32 在 Karpo marker 位置计算输出；不得改用序列最后 token，不得删除 marker 后的 chat template token。
- 仅支持本实验的单问题纯文本请求；不支持的输入明确报错。
- 先通过编码一致性与小规模答案检查，再跑完整 240 条风控、104 条 SOP 质量集，记录逐例答案、概率差异、新增错误以及 BF16 历史参考。质量未达标的配置不作为性能推荐。
- 性能测试不重跑 BF16；原生 FP8 batch=1，vLLM 分别测试 max_num_seqs=1/4/8，token budget=65536，max_model_len=8192。
- vLLM 的实际 token budget 为 min(65536, max_num_seqs×8192)，避免对不可能出现的超长批次进行启动 profiling；不影响可接受请求。以原始输入长度向上取整到 256 token 的桶，配置 prefill CUDA Graph（1/2/4/8 条的桶，受 max_num_seqs 限制）。
- 两套实现使用各自 HTTP 服务，比较完整部署方案，包含前端调度和编码流水线的差异，不将全部收益归因于 GPU kernel。原生环境保留不变，vLLM 使用其发布版锁定的 Torch 依赖。
- 每个配置预热，分别测 C1 与持续 C100（60 秒发起新请求，然后排空）。保存每次请求的完整 HTTP 延迟、响应、有效性、输入 token 数；还原实际在途请求数。
- 每个场景从无失败、质量 >90% 的 vLLM 配置中选取 C100 P50 最低者，独立复测一次。复测原生 batch=1，检查时间顺序漂移。
- 如兼容性问题需要修改适配器，保留失败日志，正式测量前重新冻结最终源码。不得把启动失败或未完成的测试记为性能结果。

## 结果与资源

- 输出可复现脚本、版本与哈希、原始请求日志、逐例一致性报告、GPU 监控、中文结论。
- P50 为完整 HTTP 响应时间，不是 TTFT；同主机测量不含生产入口及特征查询。
- 合成质量仅表示已有样例的策略一致性，不代表生产准确率。
- 运行时间设置上限并上传证据。结束后停止 GPU 实例，保留模型、EBS 和原有环境；不 terminate、不删除卷。
