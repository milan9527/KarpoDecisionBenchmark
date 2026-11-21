本目录记录三阶段实际Karpo模型验证：旧输入环境对照、已完成的batch/Graph对照、固定batch 4的复核优化。

用户在执行过程中要求停止调整batch，因此应同时阅读[原计划](PLAN.zh.md)和[修订](AMENDMENT-STOP-BATCH.zh.md)。不要将未完成的batch 16启动当作性能测试结果，也不要自动恢复batch搜索。

本地复核与生成报告：

```bash
python scripts/promo_opt_verify.py
python scripts/report.py
```

复核器重新检查原始请求顺序、输入hash、真实Karpo编码hash、marker、token数、模型选择、HTTP有效性、全部尝试分母和端到端时间，并核对实际GPU引擎、原FP32头及安装的适配器源码。正式延迟以预定到达至完整HTTP返回计算；生产入口与在线特征查询不在范围内。

数据与版本：

- `inputs/old`：历史360条请求逐字节复用，并与历史真实编码逐条核对。
- `inputs/original`：新版1440条请求逐字节复用，并与此前真实编码逐条核对。
- `inputs/review-*`：保留200个特征值及策略阈值的提示版本。baseline与原tune完全相同；flat将嵌套字段改为带完整路径的平铺字段。
- `review-source-events-v1.json.gz`：新留出集的带时间戳事件。540个新实体案例，各两种候选顺序，共1080请求。种子与实体隔离，但与tune共享45种场景模板。
- `manifest-v1.json`、`frozen/v1`：第一、二阶段的输入与运行源码。
- `review-manifest-v1.json`、`review-controller-v1.json`：复核候选与选择程序在模型调用前的冻结记录。
- `results/review-selection.json`：仅根据360条tune请求选定提示的记录；之后才运行新留出集。
- `review-calibration-v1.json`：仅由原提示tune模型分数拟合的复核阈值，冻结时间早于新留出集推理。
- `calibration-runtime-v1.json`：校准代理、压测程序与阈值的源码冻结记录。
- `results/runs`：各阶段完整HTTP响应、编码校验、计时与汇总。
- `results/nodes`：各副本实际配置、环境、日志及每两秒GPU采样。
- `results/setup-failures`：保留的启动/切换失败现场，不计作已完成压测。
- `results/batch-tuning-interruption.json`：用户中止batch搜索的运行状态记录。

硬件始终为us-east-2c三台EC2、四张RTX PRO 6000 Blackwell 96GB。每卡独立模型，FP8投影、原FP32输出头，完整双向输入，无答案缓存。batch 4与8测试时的token预算分别为32768与65536；Graph的实际捕获尺寸见各`engine-config.json`。

原始模型调用由`scripts/promo_opt_runtime.py`执行，固定速率调度与编码校验复用原基准程序。复核阶段由`scripts/promo_opt_review_controller.py`执行，固定batch 4/powers，先使用共同Graph档位比较提示，再为选定提示捕获对应长度并完成正式测试。提示修改未改善效果，最终使用原提示。

校准服务验证由`scripts/promo_opt_calibrated_runtime.py`执行，复用同四个模型副本、相同batch及Graph，不重新加载模型。代理保留`answers`中的原始模型选择与概率，另在`decision`返回校准动作。只有原选择为allow且`p(review)/(p(review)+p(allow)) >= 0.1628260940455841`时转为review。该数值是归一化分数的决策阈值，不是经过真实业务校准的风险概率。详见[校准说明](CALIBRATION.zh.md)。

`python scripts/promo_opt_calibration.py`可从原始tune响应重新推导冻结阈值；`tests/test_promo_opt_calibration.py`与`tests/test_promo_opt_calibrated_proxy.py`检查边界、原始输出保留和HTTP响应结构。模型效果与性能结论来自`results/runs/review-calibrated-confirmed`的实际GPU模型HTTP调用，并由逐条复核器独立重算。运行与恢复操作保留在`operations`；下载签名未写入证据。

本轮没有停止EC2。资源状态以`operations/final-infrastructure.json`的核对时间为准；归档回读校验以`operations/archive-receipt.json`为准。
