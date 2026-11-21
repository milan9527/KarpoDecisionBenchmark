# AWS云端固定RPS：积压修复与验收

2026-11-21，复用原有CPU EC2 `i-04aa43d75617e765b`（us-east-2 / t4g.small），独立Python压测进程通过公网CloudFront调用真实六RTX模型。GPU、FP8、完整原始输入及最大batch=4保持一致。两轮最终正式验收通过，浏览器只提交和查看，不承担逐条推理上传。

测量范围为AWS压测进程计划到达至完整响应解析，包含公网入口与全部排队，不含用户本地浏览器网络。原本地浏览器积压与新云端轮次不属于同一测量位置，不以二者P50比值宣称模型加速。

## 全部云端测试轮次

下列失败、预热和正式记录均已保存在前端历史；每个压缩导出包含完整问题和逐条回答/错误。哈希与测试编号见[validation.json](validation.json)。

| 轮次 | 成功 / 计划 | P50 ms | 稳态有效RPS | 最大发送滞后ms | 全量正确率 | 判定 |
|---|---:|---:|---:|---:|---:|---|
| [risk-warmup](risk-warmup.json.gz) | 400/400 | 77.25 | 20.00 | 3.91 | 99.00% | 预热 |
| [risk-100rps-120s-r1](risk-100rps-120s-r1.json.gz) | 11,971/12,000 | 278.50 | 91.93 | 10592.73 | 98.47% | 未通过 |
| [risk-warmup-attempt-2](risk-warmup-attempt-2.json.gz) | 400/400 | 75.25 | 20.00 | 11.03 | 99.00% | 预热 |
| [risk-100rps-120s-r1-attempt-2](risk-100rps-120s-r1-attempt-2.json.gz) | 12,000/12,000 | 379.64 | 100.01 | 316.88 | 98.65% | 未通过 |
| [risk-warmup-attempt-3](risk-warmup-attempt-3.json.gz) | 400/400 | 75.32 | 20.00 | 1.62 | 99.00% | 预热 |
| [risk-100rps-120s-r1-attempt-3](risk-100rps-120s-r1-attempt-3.json.gz) | 12,000/12,000 | 253.23 | 99.70 | 134.98 | 98.74% | 未通过 |
| [risk-warmup-attempt-4](risk-warmup-attempt-4.json.gz) | 400/400 | 77.66 | 20.00 | 5.36 | 99.00% | 预热 |
| [risk-100rps-120s-r1-attempt-4](risk-100rps-120s-r1-attempt-4.json.gz) | 12,000/12,000 | 331.13 | 99.97 | 141.89 | 98.70% | 未通过 |
| [risk-warmup-attempt-5](risk-warmup-attempt-5.json.gz) | 400/400 | 73.65 | 20.00 | 1.64 | 99.00% | 预热 |
| [risk-100rps-120s-r1-attempt-5](risk-100rps-120s-r1-attempt-5.json.gz) | 12,000/12,000 | 72.25 | 99.91 | 63.29 | 98.90% | 通过 |
| [risk-100rps-120s-r2](risk-100rps-120s-r2.json.gz) | 12,000/12,000 | 318.91 | 99.67 | 64.24 | 98.69% | 通过 |
| [risk-stopped](risk-stopped.json.gz) | 197/12,000 | 73.61 | — | 2.85 | 100.00% | 提前停止 |

## 诊断与最终调整

- [原浏览器积压分析](browser-backlog-diagnosis.json)：10,000次计划、6,062次HTTP200、3,938次浏览器未发出；正确率分母与API之外的耗时分别记录。原完整记录继续保留在前端历史。
- [额外采样轮次](profile-run.json)及[CPU采样](cpu-profile.json)、[函数采样](cpu-profile-leaves.json)：HTTP处理开销较高。该轮使用采样工具，不作为最终正式验收，完整响应保存在对应编号的云端历史。
- 先启用uvloop/httptools及sniffio，再将统计移出发送循环、释放已完成任务。仍有超过100ms的发送抖动，均保留未通过判定。
- [预热分布](warmup-routing.json)显示原同负载选择偏向前几个GPU；恢复原基准的轮转规则，并复用控制线程的AWS SDK客户端。
- 最终将压测与API转发改为aiohttp连接池，保留TLS校验、2秒空闲复用期限、完整请求超时和POST不重试。最后两轮正式测试通过原有所有性能门槛，没有修改验收阈值。

## 恢复与完整性验证

第一轮浏览器上传128kbps且增加300ms延迟。第二轮浏览器断网15秒，AWS继续完成1,623次请求；刷新恢复同一任务。浏览器未逐条调用推理接口。24,000条正式响应的token哈希逐条匹配原基准；导出指标重新计算，S3历史指标与导出一致。

停止测试保留所有已完成响应及未发送数量。单元测试覆盖排队前停止、固定到达率、失败分母、独占任务、归档幂等和截断记录恢复。46项Python测试、6项固定RPS测试通过；[运行环境HTTP验证](runtime-http-check.json)确认真实CPU环境中断连不重试、原始JSON不变与完整请求超时；[匿名访问检查](auth-checks.json)确认云端接口仍需登录。

[最终代码哈希](code-hashes.json) · [部署核对](deployment.json) · [优化前代码哈希](pre-network-optimization-code-hashes.json)。每次实际安装的运行包哈希保留在[安装记录](../demo-20261121/install-commands.json)。

仅结束本测试租约，沿用10分钟自动回收；未手动停止其他演示者的实例。本轮没有启动H200，SOP公共入口需要单独验收。
