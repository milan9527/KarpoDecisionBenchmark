"""Draw the accepted eight-H200 SOP deployment using official AWS icons."""
import argparse
import copy
import html
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-sop-h200-20261120"
ASSETS = ROOT / "docs/assets"


def render():
    status = json.loads((EXP / "results/runs/h200-fp8-v2/status.json").read_text())
    health = json.loads((EXP / "results/runs/h200-fp8-v2/sweep/b4/proxy-health.json").read_text())
    assert status["confirmed"] and status["status"] == "complete"
    assert health["active"] == list(range(8))
    assert all(b["max_num_seqs"] == 4 for b in health["backends"])
    formal = [r["result"] for r in status["stages"]
              if r["phase"] == "confirmation" and r["stage"] in ("confirm", "repeat")]
    p50 = " / ".join(f"{r['p50_ms']:.2f}" for r in formal)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="1680" height="1130" viewBox="0 0 1680 1130" role="img" aria-labelledby="title desc">',
        '<title id="title">Karpo SOP：AWS 八 H200 实测部署架构</title>',
        '<desc id="desc">一台 p5en.48xlarge Spot EC2，位于俄亥俄 us-east-2a。'
        '压测客户端、最少在途请求代理和八个单卡 vLLM 副本全部同机部署。'
        '每次输入完整 200 候选，每张 H200 运行完整 Karpo 4B FP8 模型，最大 batch 4。</desc>',
        "<defs>",
    ]
    for name in ("ec2", "aws-cloud", "region"):
        source = ET.parse(ASSETS / "aws-icons" / f"{name}.svg").getroot()
        parts.append(f'<symbol id="aws-{name}" viewBox="{source.attrib["viewBox"]}">')
        for child in source:
            if child.tag.endswith("}title"):
                continue
            child = copy.deepcopy(child)
            for element in child.iter():
                if "id" in element.attrib:
                    element.set("id", f'{name}-{element.attrib["id"]}')
            parts.append(ET.tostring(child, encoding="unicode"))
        parts.append("</symbol>")
    parts.append('<marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" '
                 'orient="auto"><path d="M0 0 L9 4.5 L0 9 Z" fill="#2764a5"/></marker>')
    parts.append('</defs><g font-family="Noto Sans CJK SC, Microsoft YaHei, sans-serif">')

    def box(x, y, w, h, fill="#ffffff", stroke="#d6dee8", radius=12):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
                     f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')

    def text(x, y, value, size=19, color="#263549", weight=400):
        parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
                     f'font-weight="{weight}">{html.escape(value)}</text>')

    def icon(name, x, y, size=44):
        parts.append(f'<use xlink:href="#aws-{name}" x="{x}" y="{y}" width="{size}" height="{size}"/>')

    def line(path, arrow=True):
        parts.append(f'<path d="{path}" fill="none" stroke="#2764a5" stroke-width="2.4" '
                     'stroke-linejoin="round"' + (' marker-end="url(#arrow)"' if arrow else "") + "/>")

    box(0, 0, 1680, 1130, "#f5f7fa", "#f5f7fa", 0)
    text(40, 61, "Karpo SOP · AWS 八 H200 部署架构", 34, "#172638", 700)
    text(40, 96, "2026-11-20 实测拓扑  |  每次完整输入 200 候选  |  每卡一个独立模型副本", 20, "#526174")
    badges = [
        ("1 台 Spot EC2", "p5en.48xlarge · 8 张 H200"),
        ("每次完整输入 200 候选", "Karpo 4B 一次选择 SOP 动作"),
        ("持续 100 请求 / 秒", "两轮各 120 秒 · 零失败"),
        (f"P50 {p50} ms", "同机完整 HTTP 响应时间"),
    ]
    for index, (headline, detail) in enumerate(badges):
        x = 40 + 405 * index
        box(x, 119, 385, 76)
        text(x + 16, 150, headline, 22, "#172638", 700)
        text(x + 16, 179, detail, 17, "#526174")

    box(32, 233, 1616, 755, "#ffffff", "#647184", 4)
    icon("aws-cloud", 48, 249, 40)
    text(102, 277, "AWS Cloud", 24, "#172638", 700)
    text(1180, 277, "已完成测试的部署拓扑", 20, "#526174")
    box(56, 310, 1568, 650, "#f5fbfb", "#00a4a6", 2)
    icon("region", 70, 323, 36)
    text(118, 349, "俄亥俄 us-east-2  /  可用区 us-east-2a", 25, "#172638", 700)
    box(82, 380, 1516, 546, "#fffaf5", "#ed7100", 8)
    icon("ec2", 102, 399, 48)
    text(169, 432, "Amazon EC2 · p5en.48xlarge · 8 × NVIDIA H200", 28, "#172638", 700)
    text(102, 470, "实例 i-0c777e3714538d624  |  压测客户端、代理及全部 GPU 副本同机部署", 20, "#6a573e")

    box(115, 500, 325, 96)
    text(133, 535, "压测客户端", 23, "#172638", 700)
    text(133, 573, "完整 200 候选 · 100 RPS", 20, "#526174")
    box(585, 500, 390, 96, "#eff5ff", "#2764a5")
    text(605, 535, "最少在途请求路由", 23, "#172638", 700)
    text(605, 573, "HTTP 127.0.0.1:18750", 20, "#526174")
    box(1120, 500, 420, 96)
    text(1140, 535, "返回 SOP 决策", 23, "#172638", 700)
    text(1140, 573, "模型选择的动作及候选概率", 20, "#526174")
    line("M440 548 H581")
    line("M975 548 H1116")
    text(982, 487, "推理完成后", 17, "#526174")
    line("M780 596 V644", arrow=False)
    line("M190 644 H1492", arrow=False)
    text(103, 626, "全部连接使用同机 loopback HTTP", 18, "#2764a5")

    for replica in range(8):
        x = 105 + 186 * replica
        line(f"M{x + 85} 644 V691")
        box(x, 695, 170, 164)
        text(x + 14, 726, f"H200 · GPU {replica}", 21, "#172638", 700)
        text(x + 14, 759, f"vLLM 副本 {replica}", 20, "#172638", 600)
        text(x + 14, 789, "Karpo 4B FP8", 18, "#526174")
        text(x + 14, 817, "最大 batch = 4", 18, "#526174")
        text(x + 14, 844, f"端口 {18500 + replica}", 17, "#526174")
    text(105, 899, "每次请求由一个 GPU 副本完成，沿原路径返回代理；vLLM 0.34.0、CUDA Graph、原始 FP32 决策头、完整双向注意力。", 20, "#526174")
    text(40, 1030, "计时范围：预定到达到完整 HTTP 响应，含发送滞后、代理、排队、编码与推理；生产入口和跨区域网络未纳入。", 20, "#263549")
    text(40, 1066, "资源状态：截至 2026-11-21 04:25 UTC 的项目快照，该 H200 实例为 stopped；本图展示测试期间部署。", 19, "#526174")
    text(40, 1104, "AWS 官方架构图标 · 来源见 docs/assets/aws-icons/SOURCE.json  |  依据：vllm-sop-h200-20261120 已核验测试记录", 16, "#667386")
    parts.append("</g></svg>\n")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true", help="Also export PNG; requires CairoSVG.")
    args = parser.parse_args()
    target = ASSETS / "sop-h200-aws-architecture.svg"
    target.write_text(render())
    if args.png:
        import cairosvg
        cairosvg.svg2png(url=str(target), write_to=str(target.with_suffix(".png")), scale=2)
    print(target.relative_to(ROOT))


if __name__ == "__main__":
    main()
