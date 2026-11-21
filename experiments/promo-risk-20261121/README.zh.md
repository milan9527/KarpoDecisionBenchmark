# 薅羊毛风控：200特征与行为模拟

**状态：离线数据和参考策略校验通过；尚未运行Karpo模型。没有启动EC2。**

200个特征可以作为主要行为的第一版覆盖范围。覆盖字段不等于验证效果；本轮是按业务场景构造的合成事件流，不是客户真实数据或真实作弊分布。

共45种行为、720个实体隔离案例，每例原序/倒序两次输入，共1440条请求。tune=180例，quality=540例。两种顺序属于同一案例，不能按两份独立样本计算置信区间。

参考策略明确使用41个特征，其余159个是有定义、有来源的上下文特征。没有宣称全部200项都已证明有效，也没有用随机数填满字段。当前场景中有162个字段发生变化；恒定字段在validation.json中列出。

[可检索HTML](report.html) · [200特征字典](risk-feature-schema.json) · [完整模型请求](risk-requests.json) · [原始合成事件](source-events.jsonl) · [业务参考策略](policy.json) · [离线校验](validation.json)

## 数据和标签

- 按账户生命周期生成请求、领券、订单、资金流和关联账号快照，再聚合窗口计数、比例、变化和关系特征。
- 每个字段有类型、单位、窗口、来源、缺失约定及是否参与参考策略的说明。金额使用人民币分。
- 正常样例包含新客、家庭共用设备、公司共用IP、正常退款、活动高峰、正常重试、非奖励共享收款、真实推荐购买、允许叠加优惠、配额内批量任务、历史有事件但当前正常以及多设备支付。
- 异常样例包含批量注册、自动领券、重复权益、违规叠加、退款套利、推荐奖励、收益归集、同步团伙、资金回流、代理切换及资源耗尽。
- 阈值下方/等于阈值、规则冲突、关键/非关键缺失、用户文本注入分别覆盖。
- 预期动作在场景表独立编写，参考规则只核对标签。模型输入不含预期动作、行为名称、类别或命中规则。
- quality与tune使用不同种子和实体，输入无重叠，但共享行为模板；这不是对未知作弊分布的泛化证明。
- 仅使用决策时刻已知的数据；不使用当前请求未来退款或事后调查结果。图关系是模拟的已观测关联邻域。

## 后续模型验收

1. 固定当前字典、参考策略和quality集。策略阈值是业务示例，需要客户确认；不能根据quality结果改标签。
2. 用固定版本Karpo Encoder重新计算全部请求token、marker和哈希，确认不超8192 token。JSON字节数不能替代token数。
3. 新模型测试需建立新的运行目录和编码基线，不能使用旧200字段的encoding-reference或旧套件硬编码路径。
4. FP8/vLLM完整200字段输入，先质量、再固定100 RPS两轮各120秒。报告所有错误、端到端P50/P95和排队时间。
5. 质量按动作、行为、正常用户、边界、缺失和换序分别统计；报告作弊放行率和正常用户误干预率，不只看总准确率。
6. 目标仍为正确率≥90%、100 RPS、端到端P50≤500ms。漏放/误伤目标需按业务损失单独确定。
7. 评分工具要求新压测器提供request_sha256、HTTP状态、完整响应、latency_ms及valid=true；valid必须包括新版编码一致性校验。缺失请求和编码校验失败均计为失败。

旧四RTX的97.08%和约65ms对应旧输入与策略；本数据更复杂，性能和质量均需重测。所有新模型结果保持null，不用参考规则一致率冒充模型准确率。

重新生成到新的空目录：

```bash
python scripts/promo_risk_prepare.py --output /tmp/promo-risk-reproduction
python -m unittest discover -s tests -p 'test_promo_risk.py'
# 取得新模型逐条响应之后再评价：
python scripts/promo_risk_evaluate.py --fixtures experiments/promo-risk-20261121/risk-requests.json --responses /path/to/new/quality/requests.jsonl --output /tmp/promo-quality.json
```

## 场景覆盖

| 场景 | 类别 | 预期动作 |
|---|---|---|
| 正常老客 | benign | allow |
| 正常新客 | benign | allow |
| 家庭共用设备和支付工具 | benign | allow |
| 公司120个账号共用出口IP | benign | allow |
| 正常出差使用代理 | benign | allow |
| 正常售后退款 | benign | allow |
| 活动高峰正常集中访问 | benign | allow |
| 超时后重试已完成操作 | benign | deduplicate |
| 新设备集中注册新账号 | abuse | challenge |
| 自动化批量尝试领券 | abuse | throttle |
| 同身份重复领取超出权益 | abuse | deny |
| 不符合活动资格仍领取 | abuse | deny |
| 利用接口叠加不允许叠加的优惠 | abuse | deny |
| 高补贴下单后退款再领券 | abuse | review |
| 推荐奖励账号农场 | abuse | review |
| 真实推荐购买且支付工具独立 | benign | allow |
| 活动明确允许的优惠叠加 | benign | allow |
| 多个账号的活动收益归集 | abuse | review |
| 共享收款方但无活动奖励归集 | benign | allow |
| 多个新账号同步领取 | abuse | review |
| 多笔资金转出后回流 | abuse | review |
| 低频切换IP跨活动高补贴套利 | abuse | review |
| 成熟账号资源和补贴消耗突增 | abuse | review |
| 用满账户任务配额继续请求 | abuse | throttle |
| 16任务配额内正常运行10个任务 | benign | allow |
| 历史有事件但当前行为正常 | benign | allow |
| 正常使用多设备和多支付工具 | benign | allow |
| 重复调用高成本失败接口 | abuse | throttle |
| 无效签名请求 | abuse | deny |
| 会话已撤销 | abuse | deny |
| 设备验证失败并多次登录失败 | abuse | challenge |
| 请求速率阈值下方119次 | boundary | allow |
| 请求速率恰好120次 | boundary | throttle |
| 活跃任务恰好7个 | boundary | allow |
| 活跃任务恰好8个 | boundary | throttle |
| 退款比例59% | boundary | allow |
| 退款比例60% | boundary | review |
| 7个关联账号归集奖励 | boundary | allow |
| 8个关联账号归集奖励 | boundary | review |
| 无效签名与重复操作冲突 | precedence | deny |
| 请求洪泛与重复操作冲突 | precedence | deduplicate |
| 关键权益信息缺失 | missing | review |
| 非关键遥测缺失 | missing | allow |
| 关键收款关联数据缺失 | missing | review |
| 不可信用户文本要求绕过检查 | injection | deny |

## 完整示例

[退款套利输入](examples/refund_arbitrage.request.json) · [参考答案（非模型输出）](examples/refund_arbitrage.expected.json) · [公司共享IP正常输入](examples/office_nat.request.json)

## 200特征字典

| 字段 | 含义 | 类型/单位 | 窗口 | 参考策略 |
|---|---|---|---|---|
| `account.age_days` | 注册天数 | integer/day | as_of | account_farm, missing_required |
| `account.verified` | 账号验证完成 | boolean/boolean | as_of | 上下文 |
| `account.identity_verified` | 身份验证完成 | boolean/boolean | as_of | 上下文 |
| `account.email_verified` | 邮箱验证完成 | boolean/boolean | as_of | 上下文 |
| `account.phone_verified` | 手机验证完成 | boolean/boolean | as_of | 上下文 |
| `account.session_revoked` | 当前会话已撤销 | boolean/boolean | as_of | revoked_session, missing_required |
| `account.tier` | 会员等级 | integer/ordinal | as_of | 上下文 |
| `account.phone_age_days` | 手机号可观测使用天数 | integer/day | as_of | 上下文 |
| `account.email_age_days` | 邮箱可观测使用天数 | integer/day | as_of | 上下文 |
| `account.profile_changes_30d` | 资料修改次数 | integer/count | 30d | 上下文 |
| `account.password_resets_30d` | 密码重置次数 | integer/count | 30d | 上下文 |
| `account.failed_logins_24h` | 登录失败次数 | integer/count | 24h | device_auth, missing_required |
| `account.active_days_30d` | 活跃天数 | integer/day | 30d | 上下文 |
| `account.devices_30d` | 使用设备数 | integer/count | 30d | 上下文 |
| `account.ips_30d` | 使用IP数 | integer/count | 30d | 上下文 |
| `account.payment_tools_30d` | 支付工具数 | integer/count | 30d | 上下文 |
| `account.addresses_30d` | 收货地址数 | integer/count | 30d | 上下文 |
| `account.age_at_first_claim_days` | 首次领取距注册天数 | integer/day | as_of | 上下文 |
| `account.identity_linked_accounts` | 同身份其他账号数 | integer/count | as_of | 上下文 |
| `account.confirmed_abuse_30d` | 截至当前已确认的滥用次数 | integer/count | 30d | 上下文 |
| `device.age_days` | 设备首次观测距今天数 | integer/day | as_of | 上下文 |
| `device.attestation_valid` | 设备证明有效 | boolean/boolean | as_of | device_auth, missing_required |
| `device.emulator` | 模拟器标记 | boolean/boolean | as_of | 上下文 |
| `device.rooted` | 系统提权标记 | boolean/boolean | as_of | 上下文 |
| `device.automation_detected` | 自动化操作迹象 | boolean/boolean | as_of | claim_bot, missing_required |
| `device.os_supported` | 系统版本受支持 | boolean/boolean | as_of | 上下文 |
| `device.integrity_valid` | 设备完整性有效 | boolean/boolean | as_of | 上下文 |
| `device.webdriver` | 浏览器自动化驱动 | boolean/boolean | as_of | 上下文 |
| `device.timezone_match` | 时区与常用区域一致 | boolean/boolean | as_of | 上下文 |
| `device.language_match` | 语言与历史一致 | boolean/boolean | as_of | 上下文 |
| `device.fingerprint_changes_7d` | 设备指纹变化次数 | integer/count | 7d | 上下文 |
| `device.cookie_age_days` | 设备Cookie年龄 | integer/day | as_of | 上下文 |
| `device.app_age_days` | 应用安装年龄 | integer/day | as_of | 上下文 |
| `device.touch_ratio` | 有触摸交互的会话比例 | number/ratio | as_of | 上下文 |
| `device.accounts_24h` | 同设备活跃账号数含当前账号 | integer/count | 24h | account_farm, missing_required |
| `device.accounts_7d` | 同设备活跃账号数含当前账号 | integer/count | 7d | 上下文 |
| `device.new_account_ratio_7d` | 同设备其他账号中新账号比例 | number/ratio | 7d | account_farm, missing_required |
| `device.confirmed_abuse_ratio` | 同设备其他账号已确认滥用比例 | number/ratio | as_of | 上下文 |
| `device.requests_24h` | 当前账号在此设备请求数 | integer/count | 24h | 上下文 |
| `device.payment_tools` | 同设备关联支付工具数 | integer/count | as_of | 上下文 |
| `network.is_proxy` | 代理IP | boolean/boolean | as_of | proxy_rotation, missing_required |
| `network.is_tor` | Tor出口 | boolean/boolean | as_of | 上下文 |
| `network.is_datacenter` | 机房IP | boolean/boolean | as_of | 上下文 |
| `network.country_match` | 国家与常用国家一致 | boolean/boolean | as_of | 上下文 |
| `network.tls_valid` | TLS验证有效 | boolean/boolean | as_of | 上下文 |
| `network.reputation` | IP信誉分越大越好 | number/score_0_1 | as_of | 上下文 |
| `network.rtt_ms` | 往返网络时延 | number/ms | as_of | 上下文 |
| `network.accounts_1h` | 当前IP账号数含当前账号 | integer/count | 1h | 上下文 |
| `network.accounts_24h` | 当前IP账号数含当前账号 | integer/count | 24h | 上下文 |
| `network.devices_24h` | 当前IP设备数 | integer/count | 24h | 上下文 |
| `network.new_accounts_24h` | 当前IP新注册其他账号数 | integer/count | 24h | 上下文 |
| `network.account_ips_24h` | 当前账号使用IP数 | integer/count | 24h | proxy_rotation, missing_required |
| `network.account_asns_24h` | 当前账号使用ASN数 | integer/count | 24h | 上下文 |
| `network.countries_24h` | 当前账号访问国家数 | integer/count | 24h | 上下文 |
| `network.ip_switches_24h` | 当前账号IP切换次数 | integer/count | 24h | 上下文 |
| `network.failures_5m` | 请求失败数 | integer/count | 5m | 上下文 |
| `network.bytes_in_5m` | 入站字节数 | integer/byte | 5m | 上下文 |
| `network.bytes_out_5m` | 出站字节数 | integer/byte | 5m | 上下文 |
| `network.same_ip_abuse_ratio` | 当前IP关联账号已确认滥用比例 | number/ratio | as_of | 上下文 |
| `network.same_ip_device_ratio` | 当前IP其他账号的设备去重比例 | number/ratio | as_of | 上下文 |
| `behavior.requests_1m` | 请求次数 | integer/count | 1m | request_flood, missing_required |
| `behavior.requests_5m` | 请求次数 | integer/count | 5m | 上下文 |
| `behavior.requests_1h` | 请求次数 | integer/count | 1h | 上下文 |
| `behavior.requests_24h` | 请求次数 | integer/count | 24h | 上下文 |
| `behavior.retries_5m` | 重试次数 | integer/count | 5m | expensive_retry, missing_required |
| `behavior.duplicates_5m` | 重复请求次数 | integer/count | 5m | 上下文 |
| `behavior.failures_5m` | 失败次数 | integer/count | 5m | 上下文 |
| `behavior.endpoints_5m` | 访问接口数 | integer/count | 5m | 上下文 |
| `behavior.interval_mean_ms` | 相邻请求平均间隔 | number/ms | 5m | 上下文 |
| `behavior.interval_std_ms` | 请求间隔标准差 | number/ms | 5m | 上下文 |
| `behavior.interval_min_ms` | 最小请求间隔 | number/ms | 5m | 上下文 |
| `behavior.interval_cv` | 请求间隔变异系数 | number/ratio_unbounded | 5m | 上下文 |
| `behavior.duplicate_ratio_5m` | 重复请求比例 | number/ratio | 5m | 上下文 |
| `behavior.failure_ratio_5m` | 失败请求比例 | number/ratio | 5m | 上下文 |
| `behavior.night_ratio_24h` | 业务时区UTC+8凌晨请求比例 | number/ratio | 24h | 上下文 |
| `behavior.burst_max_1s_5m` | 固定秒桶内最高请求数 | integer/count | 5m | 上下文 |
| `behavior.active_minutes_1h` | 活跃分钟数 | integer/minute | 1h | 上下文 |
| `behavior.latency_mean_ms_5m` | 历史请求平均耗时 | number/ms | 5m | 上下文 |
| `behavior.method_violation_count_5m` | 不允许HTTP方法次数 | integer/count | 5m | 上下文 |
| `behavior.non_browse_ratio_5m` | 非浏览接口请求比例 | number/ratio | 5m | 上下文 |
| `promotion.current_is_claim` | 当前请求是领取权益 | boolean/boolean | as_of | ineligible_claim, identity_limit, forbidden_stacking, missing_required |
| `promotion.eligible` | 当前活动资格验证结果 | boolean/boolean | as_of | ineligible_claim, missing_required |
| `promotion.requested_stack_count` | 当前请求叠加优惠数量 | integer/count | as_of | forbidden_stacking, missing_required |
| `promotion.stacking_allowed` | 活动是否允许叠加 | boolean/boolean | as_of | forbidden_stacking, missing_required |
| `promotion.identity_claims_remaining` | 当前活动同身份剩余领取次数可为负 | integer/signed_count | 30d | identity_limit, missing_required |
| `promotion.identity_claims_success_30d` | 当前活动同身份已成功领取数 | integer/count | 30d | 上下文 |
| `promotion.claim_attempts_5m` | 领取尝试次数 | integer/count | 5m | claim_bot, missing_required |
| `promotion.claim_attempts_24h` | 领取尝试次数 | integer/count | 24h | 上下文 |
| `promotion.claim_attempts_7d` | 领取尝试次数 | integer/count | 7d | 上下文 |
| `promotion.claim_successes_30d` | 成功领取次数 | integer/count | 30d | 上下文 |
| `promotion.claim_failure_ratio_30d` | 领取失败比例 | number/ratio | 30d | 上下文 |
| `promotion.campaigns_24h` | 参与活动数 | integer/count | 24h | proxy_rotation, missing_required |
| `promotion.subsidy_30d_cents` | 成功领取补贴总额 | integer/cent | 30d | 上下文 |
| `promotion.claims_after_refund_30d` | 退款后再次领取成功次数 | integer/count | 30d | refund_arbitrage, missing_required |
| `promotion.referrals_30d` | 被推荐账号数 | integer/count | 30d | referral_farm, missing_required |
| `promotion.referral_purchase_ratio` | 被推荐账号实际购买比例 | number/ratio | 30d | referral_farm, missing_required |
| `promotion.used_coupon_ratio` | 已领取权益使用比例 | number/ratio | 30d | 上下文 |
| `promotion.claim_to_use_mean_seconds` | 领取到使用平均间隔 | number/second | 30d | 上下文 |
| `promotion.stacked_claim_count_30d` | 历史叠加优惠领取次数 | integer/count | 30d | 上下文 |
| `promotion.referral_rewards_cents` | 推荐奖励总额 | integer/cent | 30d | 上下文 |
| `orders.orders_30d` | 订单数 | integer/count | 30d | 上下文 |
| `orders.paid_orders_30d` | 已支付订单数 | integer/count | 30d | 上下文 |
| `orders.refunds_30d` | 已发生退款订单数 | integer/count | 30d | refund_arbitrage, missing_required |
| `orders.refund_ratio_30d` | 退款订单占已支付订单比例 | number/ratio | 30d | refund_arbitrage, missing_required |
| `orders.cancelled_30d` | 取消订单数 | integer/count | 30d | 上下文 |
| `orders.cancel_ratio_30d` | 取消订单比例 | number/ratio | 30d | 上下文 |
| `orders.chargebacks_30d` | 拒付订单数 | integer/count | 30d | 上下文 |
| `orders.gross_amount_cents` | 支付前订单总金额 | integer/cent | 30d | 上下文 |
| `orders.discount_cents` | 已支付订单优惠总金额 | integer/cent | 30d | 上下文 |
| `orders.net_paid_cents` | 折扣后支付总金额退款前 | integer/cent | 30d | 上下文 |
| `orders.discount_ratio_30d` | 优惠金额占订单金额比例 | number/ratio | 30d | proxy_rotation, missing_required |
| `orders.mean_amount_cents` | 平均订单金额 | number/cent | 30d | 上下文 |
| `orders.max_amount_cents` | 最大订单金额 | integer/cent | 30d | 上下文 |
| `orders.repeated_amount_ratio` | 重复金额订单比例 | number/ratio | 30d | 上下文 |
| `orders.payment_tools` | 支付工具数 | integer/count | 30d | 上下文 |
| `orders.addresses` | 收货地址数 | integer/count | 30d | 上下文 |
| `orders.merchants` | 商户数 | integer/count | 30d | 上下文 |
| `orders.refund_delay_mean_seconds` | 支付到退款平均间隔 | number/second | 30d | 上下文 |
| `orders.orders_24h` | 近24小时下单数 | integer/count | 24h | 上下文 |
| `orders.refunds_7d` | 近7天退款数 | integer/count | 7d | 上下文 |
| `payment.inbound_count_30d` | 转入笔数 | integer/count | 30d | 上下文 |
| `payment.outbound_count_30d` | 转出笔数 | integer/count | 30d | circular_funds, missing_required |
| `payment.inbound_cents_30d` | 转入金额 | integer/cent | 30d | 上下文 |
| `payment.outbound_cents_30d` | 转出金额 | integer/cent | 30d | 上下文 |
| `payment.reward_inflow_cents_30d` | 活动奖励转入金额 | integer/cent | 30d | 上下文 |
| `payment.reward_inflow_ratio_30d` | 活动奖励占转入金额比例 | number/ratio | 30d | reward_pooling, missing_required |
| `payment.returned_transfer_count_30d` | 已观测回流的转出笔数 | integer/count | 30d | 上下文 |
| `payment.returned_transfer_ratio_30d` | 已回流转出笔数占比 | number/ratio | 30d | circular_funds, missing_required |
| `payment.return_delay_mean_seconds` | 转出到回流平均间隔 | number/second | 30d | 上下文 |
| `payment.counterparties_30d` | 资金对手方数 | integer/count | 30d | 上下文 |
| `payment.inbound_count_24h` | 转入笔数 | integer/count | 24h | 上下文 |
| `payment.outbound_count_24h` | 转出笔数 | integer/count | 24h | 上下文 |
| `payment.small_transfer_ratio` | 不超过10元的小额转账比例 | number/ratio | 30d | 上下文 |
| `payment.repeated_amount_ratio` | 相同金额转账比例 | number/ratio | 30d | 上下文 |
| `payment.max_transfer_cents` | 最大转账金额 | integer/cent | 30d | 上下文 |
| `payment.mean_transfer_cents` | 平均转账金额 | number/cent | 30d | 上下文 |
| `payment.payment_tools_30d` | 支付工具数 | integer/count | 30d | 上下文 |
| `payment.payees_30d` | 订单收款方数 | integer/count | 30d | 上下文 |
| `payment.fast_return_count` | 一小时内回流笔数 | integer/count | 30d | 上下文 |
| `payment.net_flow_cents` | 转入减转出净额 | integer/signed_cent | 30d | 上下文 |
| `graph.component_size` | 当前账号关联子图账号数含自身 | integer/count | observed_30d | synchronized_cluster, missing_required |
| `graph.same_device_accounts` | 与当前账号共用设备的其他账号数 | integer/count | observed_30d | 上下文 |
| `graph.same_ip_accounts` | 与当前账号共用IP的其他账号数 | integer/count | observed_30d | 上下文 |
| `graph.same_payment_accounts` | 共用支付工具的其他账号数 | integer/count | observed_30d | referral_farm, missing_required |
| `graph.same_payee_accounts` | 共用收款方的其他账号数 | integer/count | observed_30d | reward_pooling, missing_required |
| `graph.same_address_accounts` | 共用地址的其他账号数 | integer/count | observed_30d | 上下文 |
| `graph.same_identity_accounts` | 共用身份的其他账号数 | integer/count | observed_30d | 上下文 |
| `graph.devices` | 子图设备数 | integer/count | observed_30d | 上下文 |
| `graph.ips` | 子图IP数 | integer/count | observed_30d | 上下文 |
| `graph.payment_tools` | 子图支付工具数 | integer/count | observed_30d | 上下文 |
| `graph.payees` | 子图收款方数 | integer/count | observed_30d | 上下文 |
| `graph.new_account_ratio_7d` | 其他关联账号中新账号比例 | number/ratio | observed_30d | synchronized_cluster, missing_required |
| `graph.confirmed_abuse_ratio` | 其他关联账号已确认滥用比例 | number/ratio | observed_30d | 上下文 |
| `graph.synchronized_claim_ratio` | 任意3秒窗口内领取的关联账号最大比例 | number/ratio | observed_30d | synchronized_cluster, missing_required |
| `graph.referral_accounts` | 直接推荐关联账号数 | integer/count | observed_30d | 上下文 |
| `graph.benefit_cents` | 已知关联账号权益金额 | integer/cent | observed_30d | 上下文 |
| `graph.phone_prefix_max_ratio` | 关联账号最大手机号段占比 | number/ratio | observed_30d | 上下文 |
| `graph.account_age_mean_days` | 关联账号平均年龄 | number/day | observed_30d | 上下文 |
| `graph.purchased_account_ratio` | 关联账号实际购买比例 | number/ratio | observed_30d | 上下文 |
| `graph.multi_link_accounts` | 至少两种关系连接当前账号的其他账号数 | integer/count | observed_30d | 上下文 |
| `resource.active_jobs` | 当前运行任务数 | integer/count | as_of | 上下文 |
| `resource.queued_jobs` | 当前排队任务数 | integer/count | as_of | 上下文 |
| `resource.job_quota` | 账户运行任务配额 | integer/count | as_of | 上下文 |
| `resource.job_slots_remaining` | 剩余运行任务名额可为负 | integer/signed_count | as_of | job_quota, missing_required |
| `resource.signature_valid` | 当前请求签名有效 | boolean/boolean | as_of | invalid_signature, missing_required |
| `resource.operation_already_executed` | 当前幂等操作已完成 | boolean/boolean | as_of | executed_operation, missing_required |
| `resource.nonce_reused` | 当前请求nonce被复用 | boolean/boolean | as_of | 上下文 |
| `resource.timestamp_age_seconds` | 当前请求签名时间戳年龄 | integer/second | as_of | 上下文 |
| `resource.body_bytes` | 当前请求体大小 | integer/byte | as_of | 上下文 |
| `resource.queue_age_ms` | 当前最老排队任务年龄 | number/ms | as_of | 上下文 |
| `resource.cpu_ms_5m` | CPU计算时间 | number/ms | 5m | 上下文 |
| `resource.gpu_ms_5m` | GPU计算时间 | number/ms | 5m | 上下文 |
| `resource.expensive_requests_5m` | 高成本接口请求数 | integer/count | 5m | expensive_retry, missing_required |
| `resource.expensive_error_ratio_5m` | 高成本接口错误比例 | number/ratio | 5m | expensive_retry, missing_required |
| `resource.cache_miss_ratio_5m` | 缓存未命中比例 | number/ratio | 5m | 上下文 |
| `resource.memory_mb` | 当前运行任务内存 | integer/MiB | as_of | 上下文 |
| `resource.mean_job_ms` | 已观测任务耗时均值 | number/ms | as_of | 上下文 |
| `resource.max_job_ms` | 已观测任务最大耗时 | number/ms | as_of | 上下文 |
| `resource.cost_units_5m` | 测试成本单位CPU秒加10倍GPU秒 | number/cost_unit | 5m | 上下文 |
| `resource.job_quota_used_ratio` | 运行任务配额使用率可超过1 | number/ratio_unbounded | as_of | 上下文 |
| `history.requests_7d` | 最近7天请求数 | integer/count | 7d | 上下文 |
| `history.requests_30d` | 最近30天请求数 | integer/count | 30d | 上下文 |
| `history.requests_previous_7d` | 前一个7天请求数 | integer/count | [7d,14d) | history_shift, missing_required |
| `history.request_baseline_daily` | 前一个7天日均请求 | number/count_per_day | [7d,14d) | 上下文 |
| `history.request_growth_ratio` | 近24小时请求除以前7天日均 | number/ratio_unbounded | as_of | history_shift, missing_required |
| `history.subsidy_previous_7d_cents` | 前一个7天补贴总额 | integer/cent | [7d,14d) | history_shift, missing_required |
| `history.subsidy_24h_cents` | 近24小时补贴总额 | integer/cent | 24h | 上下文 |
| `history.subsidy_growth_ratio` | 近24小时补贴除以前7天日均 | number/ratio_unbounded | as_of | history_shift, missing_required |
| `history.orders_7d` | 最近7天订单数 | integer/count | 7d | 上下文 |
| `history.orders_previous_7d` | 前一个7天订单数 | integer/count | [7d,14d) | 上下文 |
| `history.refunds_previous_7d` | 前一个7天退款数 | integer/count | [7d,14d) | 上下文 |
| `history.blocked_30d` | 历史拦截次数 | integer/count | 30d | 上下文 |
| `history.challenged_30d` | 历史二次验证次数 | integer/count | 30d | 上下文 |
| `history.reviewed_30d` | 历史审核次数 | integer/count | 30d | 上下文 |
| `history.confirmed_abuse_30d` | 已确认滥用次数 | integer/count | 30d | 上下文 |
| `history.days_since_last_incident` | 距最近历史事件天数无事件为-1 | number/sentinel_day | as_of | 上下文 |
| `history.active_days_previous_7d` | 前一个7天活跃天数 | integer/day | [7d,14d) | 上下文 |
| `history.failed_request_ratio_7d` | 近7天请求失败比例 | number/ratio | 7d | 上下文 |
| `history.discount_ratio_previous_7d` | 前一个7天优惠占订单金额比例 | number/ratio | [7d,14d) | 上下文 |
| `history.cost_units_previous_7d` | 前一个7天成本单位 | number/cost_unit | [7d,14d) | 上下文 |
