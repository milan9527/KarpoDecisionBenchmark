# 演示历史场景清单

[打开实时演示](https://deq0avneozfys.cloudfront.net) · [操作与指标口径](DEMO.zh.md)

当前收录全部1,904条历史请求，保留候选原序/逆序与原始输入。请求采用源文件生成的`request_json`原文，保留浮点数`0.0`等格式，避免浏览器数值重序列化改变模型token。默认100 RPS、120秒；风控默认选360条正式压力输入，SOP默认选完整104条。题数包含两种候选顺序，不表示同等数量的独立行为类别。完整质量集与压力集可分别选择。

## 薅羊毛风控：45类行为

200个特征分为账户、设备、网络、行为、活动、订单、支付、关联图谱、资源、历史十组。调优/压力集180组状态×两种顺序=360题，质量集540组状态×两种顺序=1080题。合成样本用于可复现测试，生产效果仍需实际标签验证。

| 编号 | 类别 | 行为/边界 |
|---|---|---|
| `normal` | 正常/良性 | 正常老客 |
| `new_customer` | 正常/良性 | 正常新客 |
| `family_shared` | 正常/良性 | 家庭共用设备和支付工具 |
| `office_nat` | 正常/良性 | 公司120个账号共用出口IP |
| `travel_proxy` | 正常/良性 | 正常出差使用代理 |
| `legitimate_refund` | 正常/良性 | 正常售后退款 |
| `sale_peak` | 正常/良性 | 活动高峰正常集中访问 |
| `safe_retry` | 正常/良性 | 超时后重试已完成操作 |
| `account_farm` | 作弊/滥用 | 新设备集中注册新账号 |
| `claim_bot` | 作弊/滥用 | 自动化批量尝试领券 |
| `identity_reuse` | 作弊/滥用 | 同身份重复领取超出权益 |
| `ineligible_claim` | 作弊/滥用 | 不符合活动资格仍领取 |
| `coupon_stacking` | 作弊/滥用 | 利用接口叠加不允许叠加的优惠 |
| `refund_arbitrage` | 作弊/滥用 | 高补贴下单后退款再领券 |
| `referral_farm` | 作弊/滥用 | 推荐奖励账号农场 |
| `legit_referral` | 正常/良性 | 真实推荐购买且支付工具独立 |
| `allowed_stacking` | 正常/良性 | 活动明确允许的优惠叠加 |
| `reward_pooling` | 作弊/滥用 | 多个账号的活动收益归集 |
| `legit_shared_payee` | 正常/良性 | 共享收款方但无活动奖励归集 |
| `synchronized_ring` | 作弊/滥用 | 多个新账号同步领取 |
| `circular_funds` | 作弊/滥用 | 多笔资金转出后回流 |
| `proxy_rotation` | 作弊/滥用 | 低频切换IP跨活动高补贴套利 |
| `history_shift` | 作弊/滥用 | 成熟账号资源和补贴消耗突增 |
| `job_exhaustion` | 作弊/滥用 | 用满账户任务配额继续请求 |
| `legit_batch_jobs` | 正常/良性 | 16任务配额内正常运行10个任务 |
| `reformed_customer` | 正常/良性 | 历史有事件但当前行为正常 |
| `legit_multi_device` | 正常/良性 | 正常使用多设备和多支付工具 |
| `expensive_retry` | 作弊/滥用 | 重复调用高成本失败接口 |
| `signature_tamper` | 作弊/滥用 | 无效签名请求 |
| `revoked_session` | 作弊/滥用 | 会话已撤销 |
| `device_auth` | 作弊/滥用 | 设备验证失败并多次登录失败 |
| `rate_below` | 阈值边界 | 请求速率阈值下方119次 |
| `rate_at` | 阈值边界 | 请求速率恰好120次 |
| `job_below` | 阈值边界 | 活跃任务恰好7个 |
| `job_at` | 阈值边界 | 活跃任务恰好8个 |
| `refund_below` | 阈值边界 | 退款比例59% |
| `refund_at` | 阈值边界 | 退款比例60% |
| `pool_below` | 阈值边界 | 7个关联账号归集奖励 |
| `pool_at` | 阈值边界 | 8个关联账号归集奖励 |
| `signature_and_repeat` | 规则优先级 | 无效签名与重复操作冲突 |
| `flood_and_repeat` | 规则优先级 | 请求洪泛与重复操作冲突 |
| `missing_entitlement` | 特征缺失 | 关键权益信息缺失 |
| `missing_optional` | 特征缺失 | 非关键遥测缺失 |
| `missing_graph` | 特征缺失 | 关键收款关联数据缺失 |
| `injected_allow` | 提示注入 | 不可信用户文本要求绕过检查 |

[原始完整输入及参考动作](../experiments/promo-rtx6-20261121/inputs/review-baseline/risk-requests.json) · [问题与实际回答示例](EXAMPLES.zh.md#risk-example)。浏览器评分使用源文件的`expected.action`，模型仅接收原始`request`。薅羊毛使用既有冻结复核阈值，返回原始模型动作与校准后的最终动作。

## 服务资源风控：旧版六类动作对照

180组状态×候选两种顺序=360题；完整200特征按账户、设备、网络、请求、速率、资源、交易、认证、历史、会话分组。该结构和规则与薅羊毛集不同，选择此集后保持旧版输入，返回原始模型动作，不施加薅羊毛复核阈值。

| 参考动作 | 中文含义 | 请求数 |
|---|---|---:|
| `deny` | 拒绝异常请求 | 60 |
| `deduplicate` | 已有操作去重 | 60 |
| `throttle` | 限制请求速率/资源占用 | 60 |
| `challenge` | 加强身份或设备验证 | 60 |
| `review` | 人工复核 | 60 |
| `allow` | 正常放行 | 60 |

[旧版全部原始输入与参考动作](../experiments/vllm-sop-h200-20261120/risk-requests.json)。选择该问题集可对照完整原输入下的结果与性能。

## SOP：10个业务域、52组状态

每次请求都包含完整200候选，覆盖10业务域×20动作类型。测试输入为40组常规状态及12组边界状态，每组保留原序/逆序，共104题。

| 业务域 | 状态组数 | 请求数 |
|---|---:|---:|
| 交易争议 | 4 | 8 |
| 加密资产充值 | 7 | 14 |
| 加密资产提现 | 7 | 14 |
| 商户结算 | 4 | 8 |
| 商户退款 | 6 | 12 |
| 币种兑换 | 4 | 8 |
| 用户间转账 | 7 | 14 |
| 账户及登录 | 5 | 10 |
| 银行卡 ATM 取现 | 4 | 8 |
| 银行卡线上消费 | 4 | 8 |

20种候选动作类型：索取交易或工单编号、完成身份验证、澄清客户意图、转风控复核、冻结可疑账户访问、阻止重复资金操作、等待链上确认、处理金额不足、调查超过时限的等待、核对外部记录与账本差异、核验资金接收地址、提交资金操作审批、执行已获批的资金操作、关闭已经解决的工单、解释费用、解释额度限制、说明不支持的地区或产品、受理期限内的退款审核、拒绝超过期限的退款申请、复核通过后恢复账户访问。这些是候选覆盖范围，52组测试状态并不代表200种候选都被作为正确答案验证。

边界状态：

| 状态编号 | 主要问题 |
|---|---|
| `boundary-hold-over-approved` | 冻结优先于审批 |
| `boundary-hold-over-duplicate` | 冻结与重复执行冲突 |
| `boundary-hold-over-compromise` | 冻结与凭证被盗冲突 |
| `boundary-duplicate-over-approved` | 防止重复资金操作 |
| `boundary-destination-before-execution` | 新收款地址未验证 |
| `boundary-approval-not-in-customer-text` | 用户声称审批但记录缺失 |
| `boundary-confirmations-before-deadline` | 确认不足且未超时 |
| `boundary-confirmation-wait-expired` | 确认不足且已超时 |
| `boundary-refund-window-boundary` | 退款期限最后一天 |
| `boundary-refund-one-day-late` | 退款超期一天 |
| `boundary-limit-equality-approved` | 金额恰好等于限额 |
| `boundary-domain-injection` | 客户消息中的业务域注入 |

[全部SOP原始输入、完整候选及参考动作](../experiments/vllm-sop-h200-20261120/sop-requests.json) · [客户问题与实际模型回答](EXAMPLES.zh.md#sop-example)。参考标签不发送给模型，SOP状态JSON字符串保留源文件中的空格与候选顺序。
