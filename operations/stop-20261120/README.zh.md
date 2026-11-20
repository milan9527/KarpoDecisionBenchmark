# 2026-11-20文档更新与EC2停机

用户在四RTX风控、八H200 SOP验证完成后要求：“更新相关文档和html，ec2全都stop”。本次操作按该新指令停止项目资源，替代此前暂时保持EC2运行的安排。

本项目共有6台EC2，初始扫描覆盖账户全部20个已启用区域；5台运行中，另1台原本已停止。范围依据`Project=KarpoDecisionBenchmark`标签、Karpo实例名称及本项目已知实例ID确定。停止采用EC2 `StopInstances`，保留实例与EBS。

- [停机前全区域清点](inventory-before.json)
- [AWS停止请求响应](stop-responses.json)
- [H200关机日志摘录](h200-stop-diagnostic.json)
- [H200补发强制stop记录](h200-force-stop.json)
- [全部stopped的最终核对](infrastructure-stopped.json)
- [20个区域最终复查与Spot请求状态](all-regions-final.json)
- [风控运行时参考价格](risk-pricing.json)

根目录`report.html`、`RESULTS.zh.md`、`data.json`改为当前双场景总览。H200专项报告和复现说明增加最新停机状态；历史测试结果、原始请求和冻结运行源码保持不变。按需与Spot费用均为恢复运行时的参考计算费用，已停止的实例不再产生实例计算费用，EBS和快照等保留资源仍可能计费。

H200普通stop后超过8分钟仍处于`stopping`；控制台已记录操作系统`Power down`。因此只对该实例补发`Force=True`的stop请求，最终状态仍以AWS查询返回`stopped`为准。没有执行terminate或删除磁盘。

原始H200封存清单及S3备份对应Git提交`ccf3c30`。修改前的文档与生成器在`previous/`保留原仓库相对路径，校验清单为[previous/snapshot.json](previous/snapshot.json)。因此复核旧封存清单时，对已更新文档使用`previous/`副本，对其余文件使用原路径；本次发布另附独立清单和备份记录。

报告可使用`python scripts/report.py`离线重新生成；H200逐条复核使用`python scripts/sop_h200_report.py`。停机操作及核对脚本为`python scripts/stop_project_ec2.py stop`和`python scripts/stop_project_ec2.py status`。恢复新实验前应新建运行目录，避免覆盖既有证据。
