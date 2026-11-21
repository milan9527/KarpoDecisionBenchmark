"""Render the deployed live demo with the vendored official AWS service icons."""
import argparse
import base64
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parts = [
    '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1080" viewBox="0 0 1600 1080" role="img">',
    '<title>Karpo 实时演示：CloudFront OAC、私有 ALB、GPU 启停</title>',
    '<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#327594"/></marker>'
    '<marker id="stop" markerWidth="9" markerHeight="9" refX="8" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#c36c26"/></marker></defs>',
    '<rect width="1600" height="1080" fill="#f4f7fa"/>',
    '<g font-family="Noto Sans CJK SC,Microsoft YaHei,Arial,sans-serif">',
]


def text(x, y, value, size=18, color="#334a5a", weight=400):
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(value)}</text>')


def box(x, y, w, h, title, lines, icon=None):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="white" stroke="#c7d7e2" stroke-width="2"/>')
    if icon:
        encoded = base64.b64encode((ROOT / f"docs/assets/aws-icons/{icon}.svg").read_bytes()).decode()
        parts.append(f'<image x="{x+16}" y="{y+17}" width="48" height="48" href="data:image/svg+xml;base64,{encoded}"/>')
    text(x + (78 if icon else 20), y+39, title, 21, "#172e40", 650)
    for i, line in enumerate(lines):
        text(x+20, y+84+i*27, line, 17)


def arrow(points, label=None, at=None, stop=False):
    color = "#c36c26" if stop else "#327594"
    extra = ' stroke-dasharray="8 5"' if stop else ""
    parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"{extra} marker-end="url(#{ "stop" if stop else "arrow"})"/>')
    if label:
        text(*at, label, 16, color)


text(50, 57, "Karpo · 实时演示部署", 32, "#122c3d", 700)
text(50, 93, "前端独立在线；按场景启动 GPU；最后一个演示会话结束 10 分钟后自动停止实例", 20)
parts.append('<rect x="290" y="310" width="1260" height="245" rx="16" fill="#edf5fb" stroke="#6b97b7" stroke-width="2"/>')
text(650, 342, "AWS us-east-2 · 演示控制服务与主要 GPU", 18, "#31627f", 650)
parts.append('<rect x="1100" y="580" width="450" height="177" rx="16" fill="#f8f1e8" stroke="#c99855" stroke-width="2"/>')
text(1120, 610, "AWS us-east-1 · 复用已有两张 RTX", 18, "#90602e", 650)
box(50, 160, 205, 150, "演示浏览器", ["用户名 + 密码登录", "多问题 / 1–100并发", "完整回答 / 性能指标"])
box(340, 150, 315, 135, "Amazon CloudFront", ["HTTPS 同域入口", "静态页面与 /api/* 分流"], "cloudfront")
box(850, 145, 365, 145, "Amazon S3", ["私有 Bucket · Block Public Access", "仅 CloudFront OAC 可读 site/*"], "s3")
box(340, 375, 270, 145, "Internal ALB", ["CloudFront VPC origin", "仅私有源安全组可访问"], "alb")
box(725, 375, 305, 145, "EC2 · 演示 API", ["t4g.small · FastAPI · 常驻", "鉴权 / 就绪检查 / 转发 / 校准"], "ec2")
box(1130, 365, 390, 170, "GPU EC2 · 按场景启动", [
    "风控：3 台 EC2 · 4 × RTX PRO 6000",
    "SOP：1 台 p5en · 8 × H200",
    "每卡完整 Karpo 4B / vLLM FP8"], "ec2")
box(1130, 625, 390, 112, "GPU EC2 · 风控其余副本", ["2 台 EC2 · 2 × RTX PRO 6000"], "ec2")
box(50, 840, 260, 140, "EventBridge", ["每分钟执行一次", "独立于页面和 API 进程"], "eventbridge")
box(385, 840, 270, 140, "Lambda · 回收", ["校验持久化截止时间", "仅 StopInstances 白名单"], "lambda")
box(755, 840, 330, 140, "DynamoDB · 会话", ["600 秒空闲 / 结束倒计时", "多演示者租约与在途请求保护"], "dynamodb")
arrow("255,222 340,222", "HTTPS", (265, 209))
arrow("655,210 850,210", "OAC · SigV4", (682, 195))
arrow("475,285 475,375", "/api/*", (488, 317))
arrow("610,447 725,447", "HTTP :8000", (615, 431))
arrow("1030,425 1130,425", "TLS :19443", (1037, 407))
arrow("1030,485 1070,485 1070,680 1130,680", "固定 EIP → TLS", (890, 649))
arrow("880,520 880,840", "会话 / 启停状态", (895, 711))
arrow("310,911 385,911")
arrow("655,912 755,912", "读取 / CAS", (658, 895))
arrow("520,840 520,793 1105,793 1105,510 1130,510", "到期后停止指定 GPU EC2", (610, 782), stop=True)
arrow("1105,700 1130,700", stop=True)
text(50, 1032, "所有模型副本健康检查通过后才允许推理。倒计时到期后，调度最多约 60 秒再发出停机请求；EC2 随后进入 stopped。", 18)
text(50, 1060, "风险场景合计 6 × RTX；SOP 合计 8 × H200。API、ALB、CloudFront、S3 保持在线。官方 AWS 图标来源随仓库保留。", 17)
parts.append("</g></svg>")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true")
    args = parser.parse_args()
    target = ROOT / "docs/assets/demo-aws-architecture.svg"
    target.write_text("\n".join(parts) + "\n")
    if args.png:
        import cairosvg
        cairosvg.svg2png(url=str(target), write_to=str(target.with_suffix(".png")), scale=2)
    print(target)
