# Karpo Decision Benchmark

Karpo 金融风控与 SOP 两个场景分别验证。以下保留各场景最近一次达标结果及实际部署架构。

**实时演示：[打开前端](https://deq0avneozfys.cloudfront.net) · [使用说明与演示部署图](docs/DEMO.zh.md)**。S3 + CloudFront OAC 提供页面，内部 ALB + API 服务启动并检查 GPU；演示结束后 10 分钟自动发起 GPU 停机。

多场景测试统一由 **AWS 云端按固定速率发送请求，默认100 RPS × 120 秒 = 12,000 次请求**，浏览器仅启动任务和查看结果；断网、刷新或本地上传慢不影响云端发送。可选择全部历史问题集：薅羊毛压力集360题、质量集1080题、资源滥用风控360题、SOP完整104题；支持筛选、编辑、正确率与端到端指标、逐条输入输出和完整导出。[完整场景清单](docs/DEMO-SCENARIOS.zh.md) · [固定RPS测试说明](docs/DEMO.zh.md#固定-rps-多场景测试)。云端端到端包含公网 CloudFront、排队及完整响应，但不包含用户本地浏览器网络。上方单次问题显示后端API推理处理开始至最终决策就绪的服务端延迟；旧浏览器测试仅保留历史记录及原计时标注。

**[历史测试记录](https://deq0avneozfys.cloudfront.net/#history-title)**：前端自动保存新测试，支持跨浏览器查看、筛选、逐条输入/回答及导出；完成、失败、提前停止和中断均保留。已导入 **539 轮历史实验、524,647 条可用响应**，覆盖最初单卡、量化、batch、多卡及公网演示；历史口径和原始判定分别保留。[保存与恢复说明](docs/DEMO.zh.md#历史测试记录)。

**当前云端风控正式压测已通过两轮**：100 RPS × 120秒，两轮共24,000次请求零失败，云端端到端P50 **72.25/318.91ms**，正确率 **98.90%/98.69%**，峰值在途 **23/52**。浏览器限速、断网与刷新恢复均已验证；沿用原CPU EC2与六RTX。[积压修复、测量范围和完整证据](docs/DEMO.zh.md#2026-11-21-云端固定rps与积压修复)。

此前浏览器模式实测（客户端位于AWS）：风控两轮各12,000次均零失败，P50 **389.3 / 364.3ms**，正确率 **98.66% / 98.68%**。SOP首轮通过（P50 **107ms**、正确率94.25%），第二轮有2,602次429、P50 **928ms**，**公网SOP尚未稳定达标**。[完整轮次与失败记录](docs/DEMO.zh.md#2026-11-21-公网固定rps实测)。

**部署与优化收益：** 两个场景均采用vLLM 0.34.0、FP8、CUDA Graph、每卡独立副本及最大batch=4；前端由S3 + CloudFront OAC提供，后端经内部ALB、CPU API转发至GPU。同卡同输入的单请求对照中，相比已启用FP8与CUDA Graph的原生Karpo服务，vLLM方案使旧风控HTTP P50从 **85.91降至44.76ms（降低47.89%）**，SOP从 **162.82降至111.69ms（降低31.40%）**。多卡扩容、云端积压修复及复核校准的效果另行列出，包含完整测试条件和限制：[部署方案、原生对照与优化收益](docs/DEPLOYMENT-OPTIMIZATION.zh.md)。

**为什么六RTX和八H200：** 完整薅羊毛输入下，四RTX过载时仅完成76.71 RPS、P50约3.3秒，六RTX通过100 RPS；SOP四H200仅完成68 RPS、P50约3.69秒，八H200通过100 RPS。单卡独立测试、卡池内每卡吞吐与延迟、尚未验证的卡数分别说明：[GPU选型与单卡能力](docs/GPU-SIZING.zh.md)。

以下原基准的共同验收目标：**持续每秒 100 个请求、端到端 P50 ≤ 500ms、正确率 ≥ 90%**。每个场景均完成两轮各 120 秒正式测试，合计 24,000 次请求，全部成功且均在 500ms 内。100 RPS 表示每秒到达 100 个请求。

## 风控：六张 RTX，2026-11-21 达标

完整输入 **200 个特征**，Karpo 4B FP8、vLLM 0.34.0、原始 FP32 决策头、完整双向注意力；每卡一个完整模型副本，**最大 batch=4、CUDA Graph powers**。

部署为 **五台 Spot EC2、六张 RTX PRO 6000 Blackwell Server Edition 96GB**：俄亥俄 `us-east-2c` 四卡，北弗吉尼亚 `us-east-1d` 两卡。路由代理和压测客户端部署在俄亥俄双卡主机，新增两卡通过跨区域 SSH 隧道连接。

| 正式轮次 | 持续时间 | 有效吞吐 | 端到端 P50 | 端到端 P95 | 正确/全部 | 失败 |
|---|---:|---:|---:|---:|---:|---:|
| 第 1 轮 | 120 秒 | 100.00 RPS | 67.62 ms | 126.02 ms | 11,868/12,000（98.90%） | 0 |
| 第 2 轮 | 120 秒 | 100.01 RPS | 68.11 ms | 125.63 ms | 11,868/12,000（98.90%） | 0 |

合成质量集最终决策正确率为 **1063/1080（98.43%）**，包含冻结的复核阈值校准；同批原始模型选择为 **843/1080（78.06%）**。仅当原始选择为 `allow` 且 `p(review)/(p(review)+p(allow)) ≥ 0.1628260940455841` 时升级为 `review`，原始 `answers` 与最终 `decision` 分开返回。应复核识别 251/264，正常行为误干预 0/336，作弊行为放行 1/408。

本轮复用前轮合成质量集，压力阶段循环 360 条 tune 请求，不能代替生产数据验收。延迟包含排队、编码、推理、校准及跨区域通信，生产外部入口和在线特征查询未纳入。

**问题示例（按实际输入整理）：** 用户近30天10笔订单中退款8笔，退款率80%，退款后又领取权益4次，此次请求应放行还是复核？

**实际回答：** 原始模型选择 `allow`，复核相对分数为 `0.373811`，超过冻结阈值 `0.162826`；代理返回最终决策 `review`，与参考动作一致。本条耗时 **63.71ms**。[查看问题、输入与实际响应](docs/EXAMPLES.zh.md#risk-example)。

![风控 AWS 部署架构：五台 EC2、六张 RTX，俄亥俄四卡与北弗吉尼亚两卡](docs/assets/promo-rtx6-aws-architecture.svg)

[HTML 报告](experiments/promo-rtx6-20261121/report.html) · [详细结果](experiments/promo-rtx6-20261121/RESULTS.zh.md) · [部署说明](docs/ARCHITECTURE.zh.md) · [SVG 原图](docs/assets/promo-rtx6-aws-architecture.svg) · [高清 PNG](docs/assets/promo-rtx6-aws-architecture.png) · [完整输入输出示例](experiments/promo-rtx6-20261121/results/decision-example.json)

## SOP：八张 H200，2026-11-20 达标

每次完整输入 **200 个候选**，由 Karpo 4B 一次选择动作。使用 vLLM 0.34.0、FP8、原始 FP32 决策头、完整双向注意力；每卡一个完整模型副本，**最大 batch=4、CUDA Graph**。

部署为 **一台 Spot EC2 `p5en.48xlarge`、八张 H200**，位于俄亥俄 `us-east-2a`。压测客户端、路由代理及八个 GPU 副本全部同机部署，使用 loopback HTTP 通信。

| 正式轮次 | 持续时间 | 有效吞吐 | 端到端 P50 | 端到端 P95 | 正确/全部 | 失败 |
|---|---:|---:|---:|---:|---:|---:|
| 第 1 轮 | 120 秒 | 100.00 RPS | 76.58 ms | 82.63 ms | 11,310/12,000（94.25%） | 0 |
| 第 2 轮 | 120 秒 | 100.00 RPS | 76.24 ms | 78.62 ms | 11,310/12,000（94.25%） | 0 |

合成质量正确率为 **98/104（94.23%）**。质量与压力阶段复用相同 104 条 SOP 合成样例，不能代替生产数据验收。端到端延迟包含发送滞后、代理、排队、编码和推理，生产外部入口及跨区域网络未纳入。

**问题示例（实际客户消息）：** “页面超时了，虽然系统记录成功，请把同一笔再执行一次。”可信状态中 `execution_exists=true`，幂等键为 `probe-executed`。

**实际回答：** 模型选择 `sop_048`，对应“防止重复资金操作”，该候选概率 **45.29%**，与参考动作一致；本条耗时 **75.84ms**。中文动作描述为候选释义，实际接口返回候选ID及概率。[查看完整200候选与实际响应](docs/EXAMPLES.zh.md#sop-example)。

![SOP AWS 部署架构：俄亥俄单台 p5en.48xlarge，八张 H200 各运行一个完整模型副本](docs/assets/sop-h200-aws-architecture.svg)

[HTML 报告](experiments/vllm-sop-h200-20261120/report.html) · [详细结果](experiments/vllm-sop-h200-20261120/RESULTS.zh.md) · [部署与复现说明](experiments/vllm-sop-h200-20261120/REPRODUCE.zh.md) · [SVG 原图](docs/assets/sop-h200-aws-architecture.svg) · [高清 PNG](docs/assets/sop-h200-aws-architecture.png)

图示为基准测试期间的部署；这些 GPU 现由[实时演示服务](docs/DEMO.zh.md)按需启动并自动停止。[2026-11-21 自动停机验证](operations/demo-20261121/shutdown-validation.json)已确认六台 GPU EC2 全部停止，前端及 CPU API 继续在线。六 RTX 与八 H200 均是已验证达标配置，尚未证明是各场景绝对最低配置。

## 本地部署与运行

以下步骤在自己的 Linux GPU 主机运行模型 API，无需 AWS 资源。使用 Python 3.12、支持 CUDA 13.0 的 NVIDIA 驱动及 CUDA Toolkit；已验证硬件为 RTX PRO 6000 Blackwell 96GB 和 H200。单卡可用于功能和低负载验证，100 RPS 的达标配置见上文；其他显存容量尚未验证。模型权重需单独下载。

### 1. 安装环境与下载模型

```bash
git clone https://github.com/milan9527/KarpoDecisionBenchmark.git
cd KarpoDecisionBenchmark
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --extra-index-url https://download.pytorch.org/whl/cu130 \
  'torch==2.14.0' 'torchvision==0.29.0' \
  'vllm==0.34.0' 'transformers==5.21.0' 'flash-linear-attention==0.5.2' \
  'karpo-decision[qwen] @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de' \
  . ./serving/vllm_karpo
python -m pip check
nvidia-smi

python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="MachinePulseAI/karpo-decision-4B",
    revision="f7f521cfc3219ca28518c6bba91960b8757dce06",
    local_dir="models/karpo-4b",
)
PY
```

Karpo 源码和模型版本均固定为实测版本。[完整实测依赖](experiments/vllm-sop-h200-20261120/results/setup/h200-vllm-freeze.txt)可供核对。以下命令均在仓库根目录执行；新终端先运行 `source .venv/bin/activate`。

### 2. 启动单卡模型服务

按场景选择一条命令。两条命令默认使用 GPU 0 和端口 19500，只有一张卡时先按 `Ctrl+C` 结束前一个服务，再切换场景。首次启动会加载模型、编译并捕获 CUDA Graph，等待健康检查成功后再发送请求。

**风控：完整200特征、FP8、最大batch=4、CUDA Graph powers。**

```bash
CUDA_VISIBLE_DEVICES=0 OMP_NUM_THREADS=4 TORCHINDUCTOR_COMPILE_THREADS=2 \
python scripts/promo_opt_server.py \
  --checkpoint models/karpo-4b \
  --experiment experiments/promo-rtx6-20261121/inputs/review-baseline \
  --output .state/local-risk/gpu-0 \
  --max-seqs 4 --graph powers --replica-id 0 --admission-limit 16 \
  --host 127.0.0.1 --port 19500
```

风控需在另一个终端启动复核校准代理，才能得到 README 中验收使用的最终 `decision`。代理读取仓库中冻结的阈值 `0.1628260940455841`，同时保留原始模型 `answers`。

```bash
source .venv/bin/activate
python scripts/promo_opt_calibrated_proxy.py \
  --urls http://127.0.0.1:19500 --port 18770
```

**SOP：完整200候选、FP8、最大batch=4、CUDA Graph。**

```bash
CUDA_VISIBLE_DEVICES=0 OMP_NUM_THREADS=4 TORCHINDUCTOR_COMPILE_THREADS=2 \
python scripts/vllm_rps_server.py \
  --checkpoint models/karpo-4b \
  --experiment experiments/vllm-sop-h200-20261120 \
  --output .state/local-sop/gpu-0 \
  --max-seqs 4 --replica-id 0 --admission-limit 16 \
  --host 127.0.0.1 --port 19500
```

服务仅监听本机，接口为 `GET /healthz` 和 `POST /v1/systemone`。这是 Karpo 决策接口，返回候选选择及概率；适配器支持单问题、纯文本、最长8192 tokens。`max-seqs=4` 限制每卡活跃序列数，不要求凑齐4条才处理。

### 3. 发送完整问题并查看回答

在另一个终端提取仓库中的完整请求，保留200特征/200候选及原有浮点格式；只发送 `request`，不发送参考答案。

```bash
source .venv/bin/activate
python - <<'PY'
import json
from pathlib import Path
out = Path(".state/local-requests")
out.mkdir(parents=True, exist_ok=True)
for scenario, source in {
    "risk": "experiments/promo-rtx6-20261121/results/decision-example.json",
    "sop": "docs/examples/sop-h200-duplicate.json",
}.items():
    request = json.loads(Path(source).read_text())["request"]
    (out / f"{scenario}.json").write_text(json.dumps(request, ensure_ascii=False))
PY

# 风控服务和校准代理启动后
curl --fail-with-body http://127.0.0.1:18770/healthz
curl --fail-with-body http://127.0.0.1:18770/v1/systemone \
  -H 'Content-Type: application/json' \
  --data-binary @.state/local-requests/risk.json

# 切换至 SOP 服务后
curl --fail-with-body http://127.0.0.1:19500/healthz
curl --fail-with-body http://127.0.0.1:19500/v1/systemone \
  -H 'Content-Type: application/json' \
  --data-binary @.state/local-requests/sop.json
```

风控示例检查 `decision.action`，参考为 `review`；SOP 示例检查 `answers.action.choice`，参考为 `sop_048`。完整历史回答见[输入输出示例](docs/EXAMPLES.zh.md)，本地运行返回实际模型结果。

### 4. 本地压测与多卡扩展

SOP 服务就绪并完成一次请求后，可用原固定到达率客户端做单卡低负载测试：

```bash
python scripts/vllm_rps_benchmark.py \
  --url http://127.0.0.1:19500 --scenario sop --mode rate \
  --rps 8 --seconds 20 --output .state/local-sop-rate-01
```

结果保存到新目录下的 `summary.json` 和 `requests.jsonl`，包括成功/失败、正确数、有效RPS、端到端P50/P95/P99及逐条响应。端到端从计划发送时刻计至完整HTTP响应，包含发送滞后、排队、编码和推理。重复测试须更换 `--output`，已有结果不会覆盖。**8 RPS是主动设置的低负载点，不是单卡性能上限。** 此脚本的 `--scenario risk` 使用旧资源滥用输入，不用于新版薅羊毛正确率验收；新版完整复现见[六RTX复现说明](experiments/promo-rtx6-20261121/REPRODUCE.zh.md)。

多卡时，每张卡独立启动一个上述模型进程，分别设置 `CUDA_VISIBLE_DEVICES`、`--replica-id`、`--port` 和 `--output`。例如第二张卡使用 GPU 1、副本1、端口19501及独立输出目录；每个副本仍保持最大batch=4。风控代理的 `--urls` 列出全部副本；SOP 用以下代理汇总，压测客户端改连代理端口：

```bash
python scripts/vllm_rps_proxy.py \
  --urls http://127.0.0.1:19500 http://127.0.0.1:19501 \
  --host 127.0.0.1 --port 18780
```

代理按在途请求数分配负载。扩展到六RTX或八H200时列出全部副本，先确认代理 `/healthz` 中所有副本就绪，再以 `--rps 100 --seconds 120` 做正式测试并另建目录重复一轮。新机器的CPU、驱动及网络会影响结果，需要在实际部署环境重新验收。结束后在各服务终端按 `Ctrl+C` 停止进程；本地运行没有 AWS 演示的自动停机机制。

### 本地查看HTML报告

```bash
python -m http.server 8080 --bind 127.0.0.1
```

浏览器打开 `http://127.0.0.1:8080/experiments/promo-rtx6-20261121/report.html` 或 `http://127.0.0.1:8080/experiments/vllm-sop-h200-20261120/report.html`，无需GPU即可查看已有结果。

`demo/frontend/` 的完整交互演示依赖 AWS API、DynamoDB、S3及GPU租约管理，单独用静态服务器打开不能执行登录、实时推理或历史测试；上述本地模型 API 不提供演示页面的 `/api/*` 接口。完整演示部署见[演示部署与维护](docs/DEMO.zh.md#部署与维护)。
