# Karpo风控与SOP：当前验收结果

**新版薅羊毛风控未通过验收：质量正确率78.15%，100 RPS性能未通过。**

## 本轮配置与质量

原Karpo 4B、vLLM 0.34.0、FP8，四张RTX PRO 6000 Blackwell 96GB全部位于俄亥俄us-east-2c。一台g7e.12xlarge提供两卡，两台g7e.2xlarge各提供一卡；每卡独立模型副本，最大活跃序列数4。H100与H200均未参与本轮。

完整200特征、45类合成行为。quality集540个案例各测两种候选顺序，共1,080请求，正确844，失败0。正常行为被干预0/336；作弊行为被放行158/408。tune和quality隔离实体与种子，但共享场景模板；本结果不能代表生产准确率。

## 100 RPS实测

100 RPS指每秒固定到达100个请求。两轮各120秒，完整保留输入和候选；端到端从预定到达到完整HTTP响应，包含调度滞后、代理、排队、编码、推理和实例间私网。客户端位于双卡主机，生产入口与在线特征查询未纳入；失败全部计入验收。

| 正式轮次 | 到达RPS | 稳态有效RPS | P50 ms | P95 ms | 正确/全部 | 失败 |
|---|---|---|---|---|---|---|
| confirm | 100 | 76.71 | 3312.55 | 3340.66 | 7376/12000 | 2635 |
| repeat | 100 | 76.53 | 3323.80 | 3345.69 | 7357/12000 | 2652 |

## 当前资源与费用

资源核对时间：2026-11-21T03:03:20.839152+00:00。本项目8台EC2中3台运行，其余5台停止。本轮四RTX实例保留，运行实例继续产生费用。

本轮三台RTX实例Spot参考价合计8.0934美元/小时（720小时约5,827.25美元）；按需15.0123美元/小时（约10,808.87美元/月）。报价时间2026-11-21T03:03:20.839152+00:00，不含EBS、快照、网络、IPv4及其他保留资源；Spot价格会变化。

| 实例 | 型号 | 可用区 | 状态 | 本轮GPU |
|---|---|---|---|---|
| i-05d0c24c739f41ca5 | g7e.4xlarge | us-east-1b | stopped | 0 |
| i-02c1caf7d7ce17c76 | g7e.2xlarge | us-east-2c | running | 1 |
| i-048c3784bb87b6d86 | g7e.8xlarge | us-east-2c | stopped | 0 |
| i-05b24e3b4a7ee864d | g7e.12xlarge | us-east-2c | running | 2 |
| i-05cfc7d4fa898ee33 | g7e.2xlarge | us-east-2c | running | 1 |
| i-091371593b3c4a5ff | p5.4xlarge | us-east-2b | stopped | 0 |
| i-0c777e3714538d624 | p5en.48xlarge | us-east-2a | stopped | 0 |
| i-0de4f061c2a04fe4b | g7e.4xlarge | us-east-2c | stopped | 0 |

## 历史结果边界

旧版风控四RTX曾通过验收：合成质量97.08%，两轮100 RPS的P50为65.86/64.92ms。旧输入及策略与新版不同，该结论不能沿用为新版薅羊毛识别效果。SOP最近一次为八H200，质量94.23%，两轮100 RPS的P50为76.58/76.24ms；本轮未重测SOP。两项历史结果及当时停机记录保留在原实验目录。

## 报告与证据

[新版风控详细HTML](experiments/promo-risk-rtx4-20261121/report.html) · [新版风控文字报告](experiments/promo-risk-rtx4-20261121/RESULTS.zh.md) · [完整输入与模型实际输出](experiments/promo-risk-rtx4-20261121/results/decision-example.json) · [逐条核验记录](experiments/promo-risk-rtx4-20261121/results/verification.json) · [200特征与行为场景](experiments/promo-risk-20261121/report.html) · [旧版四RTX风控结果](experiments/vllm-rps100-20261120/RTX4.zh.md) · [SOP八H200结果](experiments/vllm-sop-h200-20261120/report.html) · [当前资源快照](experiments/promo-risk-rtx4-20261121/operations/final-infrastructure.json) · [本轮价格快照](experiments/promo-risk-rtx4-20261121/operations/pricing.json)

重新生成：`python scripts/report.py`。
