# 单次后端计时与云端统一执行

2026-11-21 已更新 https://deq0avneozfys.cloudfront.net 。

- 单次问题显示模型响应中的 `demo.service_ms`，从API推理处理开始，到服务端排队、GPU往返、模型推理和复核完成；起止点及未计入阶段见 [演示说明](../../docs/DEMO.zh.md#输入和回答)。
- 新单次历史记录的耗时与P50采用同一后端字段，浏览器往返单独保存。旧记录保留原值和计时标注；未收到后端耗时时显示「—」。
- 多场景测试统一提交AWS云端任务，移除浏览器执行选项及客户端脚本加载。旧浏览器压力测试的记录继续可查。

验证使用归档模型响应进行隔离页面回放，额外模拟350ms响应等待，检查后端耗时显示、历史保存、缺少计时、失败响应、云端任务提交及场景切换。这是界面功能验证，不是新的模型性能测试。结果见 [ui-replay.json](ui-replay.json)。

线上检查确认文件与本地一致、CloudFront缓存失效已完成、风控与SOP问题集和历史列表正常加载、手机版无横向溢出且无页面脚本错误。线上验证未发送GPU启停或测试请求，见 [deployed-check.json](deployed-check.json)。后端历史元数据代码和前端发布哈希分别记录在 [backend-deployment.json](backend-deployment.json)、[frontend-deployment.json](frontend-deployment.json)。
