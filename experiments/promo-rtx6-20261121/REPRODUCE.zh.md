# 六RTX复现与核验

固定batch=4、CUDA Graph powers；完整原输入、模型与复核阈值均不调整。六卡由原us-east-2c四卡和新增us-east-1d两卡组成，每卡一个完整Karpo 4B FP8副本。区域调整原因、申请失败记录和实际资源位于`operations`，同区域容量不足时没有停止原四卡。

本地核验和重建报告：

```bash
python scripts/promo_rtx6_verify.py
python scripts/report.py
```

`manifest-v1.json`冻结运行源码、原始请求、真实编码参考、部署拓扑和阈值。`inputs/review-baseline`逐字节复用上一轮数据；此为硬件扩容复测，未引入新的质量留出集。原质量集540个案例各测两种候选顺序，共1080条请求，与tune共享45种合成模板。

`scripts/promo_rtx6_runtime.py`复用原请求编码、模型服务、校准代理和固定速率调度，仅将副本数量改为6，并锁定batch与Graph。`scripts/promo_rtx6_controller.py`等六个模型健康后才启动压测。新增两卡通过SSH隧道访问，跨区域传输计入端到端时间。

原始模型结果位于响应`answers`，校准结果在`decision`。只有原模型选择allow且`p(review)/(p(review)+p(allow)) >= 0.1628260940455841`时升级为review；阈值在上一轮tune上拟合，本轮不再调整。正确率必须明确区分原始模型选择与最终决策。

`results/runs/rtx6-calibrated-v1`保留每次实际HTTP响应、编码hash、marker、token数、预定到达/发出/结束计时及状态；失败计入全部尝试分母。正式测试为两轮各120秒持续100 RPS，并非持续100请求在途。独立核验器重新计算汇总、检查六卡实际引擎、FP32输出头、双向注意力、软件版本和GPU采样。

`results/nodes`为六个GPU的实际引擎和运行日志。新增节点的完整模型权重hash在`results/setup`，与原四卡冻结权重一致；原四卡权重来源追溯至上一轮封存记录。路由单元测试`tests/test_promo_rtx6.py`只验证六路分发及原始输出保留，质量和性能结论来自实际GPU调用。

测试后EC2、模型与SSH隧道按用户要求保留。资源状态以`operations/final-infrastructure.json`时间为准；私有S3证据归档回读校验记录在`operations/archive-receipt.json`。EC2不会因本轮测试结束而停止；模型和隧道进程有六小时运行上限。
