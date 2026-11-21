# 六RTX实际模型验证：通过验收

六卡固定batch=4、CUDA Graph powers。两轮100 RPS端到端P50为67.62／68.11ms，有效吞吐为100.00／100.01 RPS，失败0／0次，整体通过验收。

合成质量集最终决策正确率98.43%（1063/1080），同批原始Karpo选择为78.06%。应复核识别251/264，正常行为误干预0/336，作弊行为放行1/408。

每卡一个完整Karpo 4B FP8副本，保留原FP32决策头、完整双向注意力、原提示及全部200特征。复核分数阈值保持0.1628260940455841，原始answers与校准后decision分开记录；最终决策效果包含分数后处理，不是原始模型argmax准确率。

| 配置 | 轮次 | 有效RPS | P50 ms | P95 ms | 失败/全部 | 正确/全部 |
|---|---|---|---|---|---|---|
| 四RTX | confirm | 76.71 | 3313.69 | 3339.00 | 2564/12000 | 77.58% |
| 四RTX | repeat | 76.71 | 3312.35 | 3340.13 | 2557/12000 | 77.65% |
| 六RTX | confirm | 100.00 | 67.62 | 126.02 | 0/12000 | 98.90% |
| 六RTX | repeat | 100.01 | 68.11 | 125.63 | 0/12000 | 98.90% |

实际部署为us-east-2c四张RTX加us-east-1d两张RTX PRO 6000 Blackwell Server Edition 96GB，共五台Spot EC2。俄亥俄Spot配额已满、四种RTX实例规格在三个可用区的按需容量均不足，因此新增两卡使用北弗吉尼亚Spot。两张远端卡经仅绑定loopback的SSH隧道访问。端到端从预定到达至完整HTTP响应，包含排队、编码、代理及跨区域SSH传输，不含生产外部入口与在线特征查询。四卡对照位于同可用区，六卡结果包含新增网络路径的影响。

[AWS部署说明](docs/ARCHITECTURE.zh.md) · [SVG原图](docs/assets/promo-rtx6-aws-architecture.svg) · [高清PNG](docs/assets/promo-rtx6-aws-architecture.png)

![六RTX AWS实测部署架构：五台EC2，俄亥俄四卡、北弗吉尼亚两卡](docs/assets/promo-rtx6-aws-architecture.svg)

代理与压测客户端位于双卡EC2 A，每张GPU运行一个完整模型副本。图示为测试期间部署；入口代理随压测结束退出，EC2保留状态以资源快照为准。

核对时间2026-11-21T04:25:48.373072+00:00：五台参与EC2保持运行、共六张RTX，其余五台项目EC2保持停止。

[完整六卡报告](experiments/promo-rtx6-20261121/RESULTS.zh.md) · [HTML](experiments/promo-rtx6-20261121/report.html)

SOP本轮未重测；历史八H200结果见[原报告](experiments/vllm-sop-h200-20261120/report.html)。
