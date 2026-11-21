# 四RTX新版风控验证复现

输入、标签、策略和运行代码在调用模型前冻结于`manifest-v1.json`及`frozen/v1/`。
完整输入由`../promo-risk-20261121/`生成；不能根据本轮错误修改quality标签后再报告同一轮正确率。

本轮实际拓扑：

| 副本 | EC2 | GPU数量 | 型号 | 可用区 |
|---|---|---:|---|---|
| 0、1 | i-05b24e3b4a7ee864d | 2 | g7e.12xlarge | us-east-2c |
| 2 | i-02c1caf7d7ce17c76 | 1 | g7e.2xlarge | us-east-2c |
| 3 | i-05cfc7d4fa898ee33 | 1 | g7e.2xlarge | us-east-2c |

均为RTX PRO 6000 Blackwell Server Edition 96GB。原Ohio单卡实例停止以释放Spot配额；
原Virginia单卡容量不足，保持停止。新建两台较小CPU规格的单卡实例，使四张卡全部集中在同一可用区。
容量尝试、启动、停机及部署命令原始记录位于`operations/`，不应直接重放其中的资源创建命令。

三台主机使用同一路径：

- Python环境：`/opt/karpo/venv-vllm034/bin/python`。
- 原Karpo 4B权重：`/opt/karpo/models/karpo-4b`。
- 本轮隔离运行目录：`/opt/karpo/promo-validation-v1`。
- 部署包SHA256：`dd0b37780b7e34fe2f1630466784ffbd5a29e48cf3c9a1f6c1e6d091a503e52b`。

从仓库根目录创建新版本包可用`python scripts/promo_rtx4_prepare.py --help`查看参数。
已有v1被冻结，修改任何运行代码或输入后应使用新的实验版本；不要覆盖原始记录。

在兼容的GPU环境中，先运行编码，再分别启动节点。以下命令展示实际v1参数；
`python`指上述虚拟环境中的解释器，同名输出目录存在时程序会拒绝覆盖：

```bash
python scripts/promo_rtx4_suite.py encode
python scripts/promo_rtx4_suite.py node --name dual-v1 --replica-ids 0 1
python scripts/promo_rtx4_suite.py node --name peer2-v1 --replica-ids 2 --bind 0.0.0.0
python scripts/promo_rtx4_suite.py node --name peer3-v1 --replica-ids 3 --bind 0.0.0.0
```

后三条分别在对应主机执行。节点默认最大活跃序列数4，端口从18500开始；跨主机端口仅允许控制实例安全组访问。
实际部署由systemd管理，节点进程最多运行3小时，EC2不会因此自动停止。

所有节点`/healthz`就绪后，在双卡主机运行：

```bash
python scripts/promo_rtx4_suite.py run --name rtx4-fp8-v1 --no-upload \
  --urls http://127.0.0.1:18500 http://127.0.0.1:18501 \
  http://172.31.42.209:18500 http://172.31.37.239:18500
```

新实例从AMI恢复时进行了只读权重预读和SHA256校验，三台主机权重完全一致。
所有预读和校验在压力测试前完成；记录见`results/setup/weights-*.json`与`prefetch-*.json`。

`--no-upload`只关闭驱动内上传，本轮由独立备份服务定期收集节点日志并上传私有S3。
所有质量与性能阶段均保存逐条HTTP结果、完整实际模型响应、编码哈希、计时和副本编号。
27,960次正式运行请求由预热80、quality 1,080、低负载800、探测2,000和两轮各12,000构成。
启动前另有4次tune冒烟请求，单独保存，不计入正式质量。

下载并核验：

```bash
python scripts/promo_rtx4_cloud.py sync
python scripts/promo_rtx4_infrastructure.py
python scripts/promo_rtx4_report.py
python scripts/report.py
```

前两条需要本项目既有AWS访问权限；第三条从原始响应独立重算结果，第四条生成总览。
资源与价格脚本只读，不会启动或停止EC2。GPU利用率每2秒采样，正式统计取每轮第30～120秒。
100 RPS性能结论以冻结计划为准，质量不合格时仍保留性能结果，不能据吞吐达标宣称风控效果达标。

用户在测试期间明确要求完成后不关机。本轮结束仅停止了备份轮询与已完成的测试驱动，
三台模型节点服务仍在运行，EC2保持运行并继续计费；最终资源状态见`operations/final-infrastructure.json`。
