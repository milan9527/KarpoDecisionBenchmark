# 四RTX优化验证：最终未通过验收

最终配置固定batch 4、CUDA Graph powers、原提示，增加复核分数阈值。实际HTTP服务的新留出集最终决策正确率98.43%（1063/1080）；同批请求的原始Karpo选择正确率为78.06%。两轮100 RPS性能未通过，计入失败的压力正确率未通过，整体未通过验收。

硬件固定为俄亥俄us-east-2c四张RTX PRO 6000 Blackwell 96GB，一台g7e.12xlarge双卡、两台g7e.2xlarge单卡，每卡独立Karpo 4B FP8副本，vLLM 0.34.0、原始FP32决策头、完整双向输入、无答案缓存。H100/H200未参与。

100 RPS为持续固定到达100请求/秒。端到端计时从计划到达至完整HTTP返回，包括排队、编码、代理及实例间私网；不含生产入口和在线特征查询。P50≤500ms、稳定有效RPS≥99、零失败，并检查尾段延迟、发送滞后和积压趋势。质量与正式压力正确率均须≥90%；失败计入分母。

| 测试 | 轮次 | 有效RPS | P50 ms | P95 ms | 失败 | 正确/全部 |
|---|---|---|---|---|---|---|
| old-control | confirm | 100.00 | 44.67 | 46.76 | 0 | 95.00% |
| old-control | repeat | 100.00 | 44.63 | 46.72 | 0 | 95.00% |
| review-confirmed | confirm | 76.76 | 3297.71 | 3335.47 | 2579 | 61.79% |
| review-confirmed | repeat | 76.62 | 3312.48 | 3338.44 | 2586 | 61.82% |
| review-calibrated-confirmed | confirm | 76.71 | 3313.69 | 3339.00 | 2564 | 77.58% |
| review-calibrated-confirmed | repeat | 76.71 | 3312.35 | 3340.13 | 2557 | 77.65% |

核对时间2026-11-21T04:03:17.765935+00:00，本项目3台EC2保持运行；按用户要求未关机。

[完整优化报告](experiments/promo-opt-20261121/RESULTS.zh.md) · [HTML](experiments/promo-opt-20261121/report.html)

SOP本轮未重测；历史八H200结果保留在[原报告](experiments/vllm-sop-h200-20261120/report.html)。
