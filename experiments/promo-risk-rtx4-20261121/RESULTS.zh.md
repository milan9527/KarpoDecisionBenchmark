# 新版薅羊毛风控：四RTX实际模型结果

**结论：未通过本轮验收。** 质量未通过，性能未通过，压力正确率未通过。

使用完整200特征、原Karpo 4B、vLLM 0.34.0、FP8、每卡一个副本、最大活跃序列数4。质量540个案例各测两种候选顺序，共1,080请求；压力使用独立实体/种子的tune样例循环。两分区共享45种合成行为模板，不能代表真实业务效果。

四张卡全部位于俄亥俄us-east-2c：原g7e.12xlarge提供两卡，两台g7e.2xlarge各提供一卡，共三台EC2。单卡实例因容量与配额约束换成较小CPU规格；压测客户端和代理位于双卡主机，通过同可用区私网访问另两台主机。

质量正确：844/1080（78.15%），失败0；有效换序对540，其中38对选择不同。

正常行为被干预：0/336；作弊行为被放行：158/408。失败另计，不能当作安全拦截；这些比例只针对本合成样例分布。

| 正式阶段 | 到达RPS | 稳态有效RPS | P50 ms | P95 ms | 有效/尝试 | 正确/尝试 | 失败 |
|---|---:|---:|---:|---:|---:|---:|---:|
| confirm | 100 | 76.71 | 3312.55 | 3340.66 | 9365/12000 | 7376/12000 | 2635 |
| repeat | 100 | 76.53 | 3323.80 | 3345.69 | 9348/12000 | 7357/12000 | 2652 |

错误放行分布：预期deny、实际allow共5条；预期review、实际allow共231条。

本轮只验证四卡、最大活跃序列数4的配置。未扫描其他batch和卡数，因此不能将本配置的结果解释为四张RTX所有配置的性能上限。增加副本本身也不能解决低负载质量集上的决策错误。

正式阶段HTTP状态：confirm 200×9365、429×2634、502×1；repeat 200×9348、429×2652。429为队列满拒绝，502为代理上游HTTP错误；均计为失败。

端到端从预定到达到完整HTTP响应，包含排队、编码、推理、代理和实际实例链路。生产入口与在线特征查询不在范围内。全部失败计入；表中延迟为有效响应分位数。

输入为3218～3315 token，已核验27960次请求和四份GPU引擎配置；没有输入裁剪、规则代替模型或答案缓存。

[完整数据](data.json) · [逐行为质量与错误](results/quality-details.json) · [完整输入及模型实际输出示例](results/decision-example.json) · [运行计划](PLAN.zh.md) · [原始数据说明](../promo-risk-20261121/README.zh.md)

## 按预期动作统计

| 预期动作 | 正确 | 请求 | 失败 |
|---|---:|---:|---:|
| allow | 432 | 432 | 0 |
| challenge | 48 | 48 | 0 |
| deduplicate | 48 | 48 | 0 |
| deny | 163 | 168 | 0 |
| review | 33 | 264 | 0 |
| throttle | 120 | 120 | 0 |

## 实际决策混淆

| 预期动作 | 实际动作 | 请求 |
|---|---|---:|
| allow | allow | 432 |
| challenge | challenge | 48 |
| deduplicate | deduplicate | 48 |
| deny | allow | 5 |
| deny | deny | 163 |
| review | allow | 231 |
| review | review | 33 |
| throttle | throttle | 120 |

## 逐行为质量

| 行为 | 正确 | 请求 | 失败 |
|---|---:|---:|---:|
| 新设备集中注册新账号（account_farm） | 24 | 24 | 0 |
| 活动明确允许的优惠叠加（allowed_stacking） | 24 | 24 | 0 |
| 多笔资金转出后回流（circular_funds） | 0 | 24 | 0 |
| 自动化批量尝试领券（claim_bot） | 24 | 24 | 0 |
| 利用接口叠加不允许叠加的优惠（coupon_stacking） | 24 | 24 | 0 |
| 设备验证失败并多次登录失败（device_auth） | 24 | 24 | 0 |
| 重复调用高成本失败接口（expensive_retry） | 24 | 24 | 0 |
| 家庭共用设备和支付工具（family_shared） | 24 | 24 | 0 |
| 请求洪泛与重复操作冲突（flood_and_repeat） | 24 | 24 | 0 |
| 成熟账号资源和补贴消耗突增（history_shift） | 0 | 24 | 0 |
| 同身份重复领取超出权益（identity_reuse） | 24 | 24 | 0 |
| 不符合活动资格仍领取（ineligible_claim） | 24 | 24 | 0 |
| 不可信用户文本要求绕过检查（injected_allow） | 19 | 24 | 0 |
| 活跃任务恰好8个（job_at） | 24 | 24 | 0 |
| 活跃任务恰好7个（job_below） | 24 | 24 | 0 |
| 用满账户任务配额继续请求（job_exhaustion） | 24 | 24 | 0 |
| 16任务配额内正常运行10个任务（legit_batch_jobs） | 24 | 24 | 0 |
| 正常使用多设备和多支付工具（legit_multi_device） | 24 | 24 | 0 |
| 真实推荐购买且支付工具独立（legit_referral） | 24 | 24 | 0 |
| 共享收款方但无活动奖励归集（legit_shared_payee） | 24 | 24 | 0 |
| 正常售后退款（legitimate_refund） | 24 | 24 | 0 |
| 关键权益信息缺失（missing_entitlement） | 12 | 24 | 0 |
| 关键收款关联数据缺失（missing_graph） | 0 | 24 | 0 |
| 非关键遥测缺失（missing_optional） | 24 | 24 | 0 |
| 正常新客（new_customer） | 24 | 24 | 0 |
| 正常老客（normal） | 24 | 24 | 0 |
| 公司120个账号共用出口IP（office_nat） | 24 | 24 | 0 |
| 8个关联账号归集奖励（pool_at） | 11 | 24 | 0 |
| 7个关联账号归集奖励（pool_below） | 24 | 24 | 0 |
| 低频切换IP跨活动高补贴套利（proxy_rotation） | 3 | 24 | 0 |
| 请求速率恰好120次（rate_at） | 24 | 24 | 0 |
| 请求速率阈值下方119次（rate_below） | 24 | 24 | 0 |
| 推荐奖励账号农场（referral_farm） | 0 | 24 | 0 |
| 历史有事件但当前行为正常（reformed_customer） | 24 | 24 | 0 |
| 高补贴下单后退款再领券（refund_arbitrage） | 0 | 24 | 0 |
| 退款比例60%（refund_at） | 0 | 24 | 0 |
| 退款比例59%（refund_below） | 24 | 24 | 0 |
| 会话已撤销（revoked_session） | 24 | 24 | 0 |
| 多个账号的活动收益归集（reward_pooling） | 7 | 24 | 0 |
| 超时后重试已完成操作（safe_retry） | 24 | 24 | 0 |
| 活动高峰正常集中访问（sale_peak） | 24 | 24 | 0 |
| 无效签名与重复操作冲突（signature_and_repeat） | 24 | 24 | 0 |
| 无效签名请求（signature_tamper） | 24 | 24 | 0 |
| 多个新账号同步领取（synchronized_ring） | 0 | 24 | 0 |
| 正常出差使用代理（travel_proxy） | 24 | 24 | 0 |

## GPU利用率

| 正式阶段 | 副本 | 平均利用率 | 峰值利用率 |
|---|---:|---:|---:|
| confirm | 0 | 98.62% | 100% |
| confirm | 1 | 98.60% | 100% |
| confirm | 2 | 99.00% | 100% |
| confirm | 3 | 99.07% | 100% |
| repeat | 0 | 98.58% | 100% |
| repeat | 1 | 98.67% | 100% |
| repeat | 2 | 99.04% | 100% |
| repeat | 3 | 99.11% | 100% |

## 实例状态

资源状态以[最终核对记录](operations/final-infrastructure.json)中的时间为准。
- i-05d0c24c739f41ca5 / g7e.4xlarge / us-east-1b / stopped
- i-02c1caf7d7ce17c76 / g7e.2xlarge / us-east-2c / running
- i-048c3784bb87b6d86 / g7e.8xlarge / us-east-2c / stopped
- i-05b24e3b4a7ee864d / g7e.12xlarge / us-east-2c / running
- i-05cfc7d4fa898ee33 / g7e.2xlarge / us-east-2c / running
- i-091371593b3c4a5ff / p5.4xlarge / us-east-2b / stopped
- i-0c777e3714538d624 / p5en.48xlarge / us-east-2a / stopped
- i-0de4f061c2a04fe4b / g7e.4xlarge / us-east-2c / stopped
