# H200 SOP复现说明

本轮使用AWS `p5en.48xlarge`，8张H200、每卡一份Karpo 4B完整模型，位于`us-east-2a`。旧RTX主机仅用于管理与备份，测试客户端、HTTP代理和全部模型计算都在H200主机内。H100没有参与本轮推理。

## 运行方式

运行输入及源码在`frozen/v2/`中，哈希清单为`manifest-v2.json`。该目录保存仓库相对路径；应将其中的文件恢复到独立的仓库副本，再运行。新运行必须使用不同的`--name`，不能覆盖既有结果。

```bash
python scripts/sop_h200_suite.py \
  --name h200-fp8-new-run --revision v2 \
  --gpus 8 --replicas 1 2 4 8 --batches 1 4
```

完整环境版本见`results/runs/h200-fp8-v2/environment.json`，每个模型的启动参数及实际配置见`results/runs/h200-fp8-v2/sweep/b4/server-*/`。本轮为vLLM 0.34.0、FP8按通道量化；保留原始FP32决策输出头，使用CUDA Graph，tensor parallel为1。`max_num_seqs=4`是每卡最大活跃序列数，不是要求每次必须等齐4条请求。

模型文件预取、环境安装、CUDA初始化和预热全部在计时前完成。模型文件预取记录位于`results/setup/h200-prefetch.json`，不计入请求延迟。

`operations/`保存本次实际使用的管理、部署、备份及资源核对脚本副本，其中路径和实例ID对应本次运行；它们是操作记录，不是可以直接在任意账户重放的通用部署工具。没有保存SSH私钥或访问令牌。

## 验收与核验

先按冻结计划完成8种配置的质量、低负载和100 RPS探测，再按最少已测副本数、最低探测P50选配置。正式确认分两轮执行，每轮120秒、12,000个请求。100 RPS是固定到达率，不是固定100个在途请求。

质量阶段和压力阶段复用相同104条SOP合成样例，没有独立生产测试集。每条输入都包含原始200候选，程序未按业务域筛选；逐条验证编码哈希、token数和marker。

```bash
python scripts/sop_h200_report.py
```

报告脚本重新核验原始响应、错误计数、时延、吞吐、配置选择和最终验收结论。它依赖本仓库保留的上一轮RTX输入和FP32输出头基线。`evidence-manifest.json`和`backup.json`记录Git提交`ccf3c30`时的测试封存及私有S3归档；本次按停机指令更新文档，修改前版本保存在仓库根目录的`operations/stop-20261120/previous/`。测试原始记录保持不变，当前文档版本应使用停机更新的清单核对。

## 当前资源状态

用户在本轮验证后要求全部EC2停止，现已执行stop，实例和EBS保留。最新状态见[停机核对记录](../../operations/stop-20261120/infrastructure-stopped.json)。停止期间不再产生实例计算费用，EBS和快照等保留资源仍可能计费。

`results/infrastructure-final.json`是停机指令之前的历史快照，记录当时仍在运行的状态；`results/pricing-final.json`是运行时参考价格。单卡、双卡和四卡阶段均按整台八卡实例计费。冻结的测试脚本原本只停止自身进程，这次EC2停机由独立的用户指令触发。

v1部署因缺少CPU测试依赖脚本而退出，未产生推理性能测量。v2补齐文件后重新冻结、通过六项CPU测试和八卡CUDA检查再开始测试。v1失败记录与v2结果分别保留。
