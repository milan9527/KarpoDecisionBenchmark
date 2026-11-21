# 四RTX优化验证：最终未通过验收

最终配置固定batch 4、CUDA Graph powers、原提示，增加复核分数阈值。实际HTTP服务的新留出集最终决策正确率98.43%（1063/1080）；同批请求的原始Karpo选择正确率为78.06%。两轮100 RPS性能未通过，计入失败的压力正确率未通过，整体未通过验收。

硬件固定为俄亥俄us-east-2c四张RTX PRO 6000 Blackwell 96GB，一台g7e.12xlarge双卡、两台g7e.2xlarge单卡，每卡独立Karpo 4B FP8副本，vLLM 0.34.0、原始FP32决策头、完整双向输入、无答案缓存。H100/H200未参与。

100 RPS为持续固定到达100请求/秒。端到端计时从计划到达至完整HTTP返回，包括排队、编码、代理及实例间私网；不含生产入口和在线特征查询。P50≤500ms、稳定有效RPS≥99、零失败，并检查尾段延迟、发送滞后和积压趋势。质量与正式压力正确率均须≥90%；失败计入分母。

## 旧输入环境对照

旧360条输入逐字节复用，真实编码与历史逐条相同。旧质量仍为97.08%；当前正式性能通过。旧输入为2263～2298 tokens，最终新版输入为3216～3315 tokens。新版原输入完整保留200个特征，batch/Graph扫描仅改服务配置。用户随后要求停止调整batch，后续固定原batch 4/powers配置继续复核优化。

## 完整新版输入：已完成的batch与CUDA Graph对照

已完成4组性能扫描，均使用tune集。剩余batch搜索按用户要求停止，已完成结果保留；固定batch 4继续后续验证。powers在不超过batch上限时覆盖1/2/4/8/16批大小，dense补全1至batch的中间档位，实际捕获token档位以engine-config.json为准。未把探测结果当作正式验收。

| batch | Graph | 有效RPS | P50 ms | 失败 | 探测通过 |
|---|---|---|---|---|---|
| 4 | dense | 77.33 | 3198.38 | 431 | 否 |
| 4 | powers | 77.42 | 3261.75 | 432 | 否 |
| 8 | dense | 77.51 | 3128.50 | 446 | 否 |
| 8 | powers | 77.16 | 3088.92 | 462 | 否 |

## 复核判断：调参集选择

提示候选分别为原描述、分条条件、自然语言条件、allow排除条件、平铺字段。全部保留200个特征值、六类动作、业务阈值及优先级；flat仅改变字段布局。用360条原tune请求选择版本，选择记录冻结后才推理新留出集。新留出集含540个案例、两种候选顺序，共1080请求；实体、种子和请求与旧集隔离，但共享45种合成场景模板，结果不能代表生产准确率。先在共同Graph配置下比较原提示与选定提示，再对最终提示单独捕获Graph并正式压测。

| 提示版本 | 调参正确率 | review正确/全部 | 平均JSON字节 |
|---|---|---|---|
| baseline | 79.17% | 14/88 | 8728 |
| split | 69.17% | 30/88 | 8563 |
| prose | 67.50% | 26/88 | 9275 |
| guard | 77.78% | 49/88 | 10322 |
| flat | 65.28% | 22/88 | 10745 |

## 复核阈值校准

提示修改均未优于原提示，因此最终保留原输入。只在原模型选择allow时，若p(review)/(p(review)+p(allow)) ≥ 0.1628260940455841，将最终决策升级为review；其他动作不变。原始answers及概率完整保留，decision单独返回。阈值仅用360条tune拟合，调参正确率98.89%，在2026-11-21T03:49:36.762319+00:00冻结，早于新留出集第一次模型推理。这是基于模型分数的决策后处理，不是原模型argmax准确率，也不是模型微调。不增加GPU调用；代理后处理计时包含读取及解析响应、计算阈值，不含最终JSON序列化。正式阶段该计时P50为0.3291／0.2469ms，完整服务开销仍以端到端测试为准。新留出集剩余错误为：应deny却选allow：4条；应review却选allow：13条。

## 独立新留出集

review为应复核类的召回计数；失败不算正确，也不当作安全拦截。

| 新留出集 | 正确率 | review正确/全部 | 正常行为被干预 | 作弊行为被放行 | 换序不一致 |
|---|---|---|---|---|---|
| 原提示/共同Graph | 78.06% | 31/264 | 0/336 | 158/408 | 35/540 |
| 原提示/正式配置 | 78.06% | 31/264 | 0/336 | 158/408 | 35/540 |
| 校准服务中的原始模型选择 | 78.06% | 31/264 | 0/336 | 158/408 | 35/540 |
| 校准服务最终决策 | 98.43% | 251/264 | 0/336 | 1/408 | 17/540 |

## 40 RPS低负载对照

同完整输入、同batch 4；此处正确率来自循环tune请求，不是独立留出集。

| 40 RPS对照 | 有效RPS | P50 ms | 失败 | 正确/全部 |
|---|---|---|---|---|
| review-confirmed | 40.00 | 62.54 | 0 | 81.25% |
| review-calibrated-confirmed | 40.00 | 62.96 | 0 | 99.00% |

## 两轮120秒正式验证

延迟分位数取有效响应，所有失败另计并导致性能验收失败。

| 测试 | 轮次 | 有效RPS | P50 ms | P95 ms | 失败 | 正确/全部 |
|---|---|---|---|---|---|---|
| old-control | confirm | 100.00 | 44.67 | 46.76 | 0 | 95.00% |
| old-control | repeat | 100.00 | 44.63 | 46.72 | 0 | 95.00% |
| review-confirmed | confirm | 76.76 | 3297.71 | 3335.47 | 2579 | 61.79% |
| review-confirmed | repeat | 76.62 | 3312.48 | 3338.44 | 2586 | 61.82% |
| review-calibrated-confirmed | confirm | 76.71 | 3313.69 | 3339.00 | 2564 | 77.58% |
| review-calibrated-confirmed | repeat | 76.71 | 3312.35 | 3340.13 | 2557 | 77.65% |

## 正式阶段GPU利用率

取每轮后90秒，每两秒采样。

| 测试 | 轮次 | 副本 | 平均GPU利用率 |
|---|---|---|---|
| old-control | confirm | 0 | 89.60% |
| old-control | confirm | 1 | 84.87% |
| old-control | confirm | 2 | 84.51% |
| old-control | confirm | 3 | 84.47% |
| old-control | repeat | 0 | 89.29% |
| old-control | repeat | 1 | 84.53% |
| old-control | repeat | 2 | 85.29% |
| old-control | repeat | 3 | 84.69% |
| review-calibrated-confirmed | confirm | 0 | 99.13% |
| review-calibrated-confirmed | confirm | 1 | 98.93% |
| review-calibrated-confirmed | confirm | 2 | 98.62% |
| review-calibrated-confirmed | confirm | 3 | 99.02% |
| review-calibrated-confirmed | repeat | 0 | 99.09% |
| review-calibrated-confirmed | repeat | 1 | 99.02% |
| review-calibrated-confirmed | repeat | 2 | 99.07% |
| review-calibrated-confirmed | repeat | 3 | 98.87% |
| review-confirmed | confirm | 0 | 99.07% |
| review-confirmed | confirm | 1 | 98.93% |
| review-confirmed | confirm | 2 | 98.89% |
| review-confirmed | confirm | 3 | 98.98% |
| review-confirmed | repeat | 0 | 99.02% |
| review-confirmed | repeat | 1 | 98.89% |
| review-confirmed | repeat | 2 | 98.87% |
| review-confirmed | repeat | 3 | 99.07% |

## EC2状态

核对时间2026-11-21T04:03:17.765935+00:00，本项目3台EC2保持运行；按用户要求未关机。

| 实例 | 类型 | 可用区 | 状态 |
|---|---|---|---|
| i-02c1caf7d7ce17c76 | g7e.2xlarge | us-east-2c | running |
| i-048c3784bb87b6d86 | g7e.8xlarge | us-east-2c | stopped |
| i-05b24e3b4a7ee864d | g7e.12xlarge | us-east-2c | running |
| i-05cfc7d4fa898ee33 | g7e.2xlarge | us-east-2c | running |
| i-05d0c24c739f41ca5 | g7e.4xlarge | us-east-1b | stopped |
| i-091371593b3c4a5ff | p5.4xlarge | us-east-2b | stopped |
| i-0c777e3714538d624 | p5en.48xlarge | us-east-2a | stopped |
| i-0de4f061c2a04fe4b | g7e.4xlarge | us-east-2c | stopped |

核验102,152次请求、30份GPU引擎配置。

[逐条核验](results/verification.json) · [完整输入输出示例](results/decision-example.json) · [性能选择](results/performance-selection.json) · [提示选择](results/review-selection.json) · [阈值冻结](review-calibration-v1.json) · [原计划](PLAN.zh.md) · [停止batch搜索的修订](AMENDMENT-STOP-BATCH.zh.md) · [校准验证说明](CALIBRATION.zh.md)
