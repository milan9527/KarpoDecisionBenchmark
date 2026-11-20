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

报告脚本重新核验原始响应、错误计数、时延、吞吐、配置选择和最终验收结论。它依赖本仓库保留的上一轮RTX输入和FP32输出头基线。`evidence-manifest.json`记录本轮最终文件哈希；`backup.json`记录私有S3归档及下载校验结果。重新生成报告会更新核验时间，因此不应将重新生成后的文件误当作原始封存版本。

## 资源保留

测试脚本完成后结束自己创建的模型和采样进程，EC2及磁盘继续保留；不会自动关闭任何EC2。单卡、双卡和四卡阶段仍按整台八卡实例计费。最终资源和价格记录分别为`results/infrastructure-final.json`、`results/pricing-final.json`。

v1部署因缺少CPU测试依赖脚本而退出，未产生推理性能测量。v2补齐文件后重新冻结、通过六项CPU测试和八卡CUDA检查再开始测试。v1失败记录与v2结果分别保留。
