#!/bin/bash
set -Eeuo pipefail
exec >>/var/log/karpo-h100-bootstrap.log 2>&1
trap '/sbin/shutdown -h now' ERR
systemd-run --unit=karpo-h100-safety-stop --on-active=60m /sbin/shutdown -h now
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv gcc git
mkdir -p /opt/karpo
python3 -m venv /opt/karpo/venv
/opt/karpo/venv/bin/pip install --quiet uv
/opt/karpo/venv/bin/uv pip install --python /opt/karpo/venv/bin/python --extra-index-url https://download.pytorch.org/whl/cu130 'karpo-decision[qwen] @ git+https://github.com/machinepulse-ai/karpo-decision@6600011eb5cf46232d056ec0717dc726146d29de' 'torch==2.14.1+cu130' 'transformers==5.21.0' 'torchvision==0.29.1+cu130' 'triton==3.8.0' 'flash-linear-attention==0.5.2' 'accelerate==1.15.0' boto3 httpx huggingface_hub
/opt/karpo/venv/bin/python - <<'INNER'
import boto3,pathlib,hashlib,tarfile
from karpo_decision.families.qwen.packed import _kernels
assert len(_kernels()) == 3
p=pathlib.Path('/tmp/karpo-h100-inputs.tar.gz')
boto3.client('s3',region_name='us-east-1').download_file('karpo-benchmark-20261119-115003-632930644527','inputs/h100-4b-20261120.tar.gz',str(p))
assert hashlib.sha256(p.read_bytes()).hexdigest()=='371b25e5879383d3c5b2cb8f49cb5eb8c5b0f283e391b3ce41ab459621fe0a62'
with tarfile.open(p) as t:t.extractall('/opt/karpo',filter='data')
pathlib.Path('/opt/karpo/models/karpo-4b').mkdir(exist_ok=True,parents=True)
INNER
mkdir -p /opt/karpo/experiments/h100-4b-20261120/results
cat > /opt/karpo/h100-launch.sh <<'INNER'
#!/bin/bash
set -Euo pipefail
export HF_HUB_DISABLE_XET=1 PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
exec >>/opt/karpo/experiments/h100-4b-20261120/results/launcher.log 2>&1
trap '/sbin/shutdown -h now' EXIT
/opt/karpo/venv/bin/python -u /opt/karpo/scripts/h100_suite.py --bucket karpo-benchmark-20261119-115003-632930644527
INNER
chmod 700 /opt/karpo/h100-launch.sh
systemd-run --unit=karpo-h100 --property=RuntimeMaxSec=50m --property='ExecStopPost=/sbin/shutdown -h now' /opt/karpo/h100-launch.sh
