"""Draw the measured six-RTX topology with locally vendored official AWS icons."""
import argparse
import copy
import html
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"
ASSETS = ROOT / "docs/assets"
SVG_NS = "http://www.w3.org/2000/svg"


def render():
    infra = json.loads((EXP / "operations/final-infrastructure.json").read_text())
    proof = json.loads((EXP / "results/verification.json").read_text())
    live = {r["id"]: r for r in infra["instances"] if r["participates"]}
    expected = {
        "i-05b24e3b4a7ee864d": ("g7e.12xlarge", "us-east-2c", 2),
        "i-02c1caf7d7ce17c76": ("g7e.2xlarge", "us-east-2c", 1),
        "i-05cfc7d4fa898ee33": ("g7e.2xlarge", "us-east-2c", 1),
        "i-09e55f0a86313912d": ("g7e.2xlarge", "us-east-1d", 1),
        "i-0b7835341b1266d7a": ("g7e.2xlarge", "us-east-1d", 1),
    }
    assert set(live) == set(expected)
    for key, values in expected.items():
        assert (live[key]["type"], live[key]["az"], live[key]["test_gpu_count"]) == values
    formal = [r for r in proof["runs"][0]["stages"] if r["name"] in ("confirm", "repeat")]
    p50 = " / ".join(f"{r['p50_ms']:.2f}" for r in formal)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="1680" height="1340" viewBox="0 0 1680 1340" role="img" aria-labelledby="title desc">',
        "<title id=\"title\">Karpo 风控六 RTX：AWS 实测部署架构</title>",
        '<desc id="desc">五台 Spot EC2，六张 RTX PRO 6000 Blackwell 96GB。'
        '俄亥俄 us-east-2c 四卡，北弗吉尼亚 us-east-1d 两卡。'
        '双卡 EC2 A 同时运行压测客户端、最少在途请求代理和复核校准。'
        '每张卡一个 Karpo 4B FP8 vLLM 副本，最大 batch 4，跨区域使用 SSH 隧道。</desc>',
        "<defs>",
    ]
    for name in ("ec2", "aws-cloud", "region"):
        source = ET.parse(ASSETS / "aws-icons" / f"{name}.svg").getroot()
        parts.append(f'<symbol id="aws-{name}" viewBox="{source.attrib["viewBox"]}">')
        for child in source:
            if child.tag == f"{{{SVG_NS}}}title":
                continue
            child = copy.deepcopy(child)
            for element in child.iter():
                if "id" in element.attrib:
                    element.set("id", f'{name}-{element.attrib["id"]}')
            parts.append(ET.tostring(child, encoding="unicode"))
        parts.append("</symbol>")
    for name, color in (("blue", "#2764a5"), ("orange", "#cb660c")):
        parts.append(f'<marker id="arrow-{name}" markerWidth="9" markerHeight="9" refX="8" '
                     f'refY="4.5" orient="auto"><path d="M0 0 L9 4.5 L0 9 Z" fill="{color}"/></marker>')
    parts.append('</defs><g font-family="Noto Sans CJK SC, Microsoft YaHei, sans-serif">')

    def box(x, y, w, h, fill="#ffffff", stroke="#d6dee8", radius=12, dash=None):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
                     f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"'
                     + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")

    def text(x, y, value, size=19, color="#263549", weight=400):
        parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
                     f'font-weight="{weight}">{html.escape(value)}</text>')

    def icon(name, x, y, size=44):
        parts.append(f'<use xlink:href="#aws-{name}" x="{x}" y="{y}" width="{size}" height="{size}"/>')

    def line(path, remote=False, arrow=True):
        color, name = ("#cb660c", "orange") if remote else ("#2764a5", "blue")
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.4" '
                     'stroke-linejoin="round"'
                     + (' stroke-dasharray="8 5"' if remote else "")
                     + (f' marker-end="url(#arrow-{name})"' if arrow else "") + "/>")

    box(0, 0, 1680, 1340, "#f5f7fa", "#f5f7fa", 0)
    text(40, 61, "Karpo 风控 · AWS 六 RTX 部署架构", 34, "#172638", 700)
    text(40, 96, "2026-11-21 实测拓扑  |  完整 200 特征  |  每卡一个独立模型副本", 20, "#526174")
    badges = [
        ("5 台 Spot EC2", "1 台双卡 + 4 台单卡"),
        ("6 张同型号 GPU", "RTX PRO 6000 Blackwell · 96GB"),
        ("持续 100 请求 / 秒", "两轮各 120 秒 · 零失败"),
        (f"P50 {p50} ms", "含排队、编码、推理及跨区域传输"),
    ]
    for index, (headline, detail) in enumerate(badges):
        x = 40 + 405 * index
        box(x, 119, 385, 76)
        text(x + 16, 150, headline, 22, "#172638", 700)
        text(x + 16, 179, detail, 16, "#526174")

    text(40, 235, "请求处理顺序", 21, "#172638", 700)
    steps = [
        ("01  原始请求", "完整 200 特征与原提示"),
        ("02  代理分发", "选择在途请求最少的副本"),
        ("03  单卡 vLLM 推理", "Karpo 4B FP8 + FP32 决策头"),
        ("04  复核阈值校准", "代理按模型概率升级部分放行"),
        ("05  返回决策", "保留 answers，另返回 decision"),
    ]
    for index, (headline, detail) in enumerate(steps):
        x = 40 + index * 326
        box(x, 254, 294, 90)
        text(x + 15, 287, headline, 20, "#172638", 700)
        text(x + 15, 319, detail, 16, "#526174")
        if index < 4:
            line(f"M{x + 294} 299 H{x + 321}")

    box(32, 388, 1616, 797, "#ffffff", "#647184", 4)
    icon("aws-cloud", 48, 404, 40)
    text(102, 432, "AWS Cloud", 24, "#172638", 700)
    text(1120, 432, "测试期间的部署 · 资源快照 04:25 UTC", 18, "#526174")
    box(56, 465, 974, 661, "#f5fbfb", "#00a4a6", 2)
    box(1070, 465, 550, 661, "#f5fbfb", "#00a4a6", 2)
    icon("region", 70, 478, 36)
    text(118, 504, "俄亥俄 us-east-2  /  可用区 us-east-2c", 23, "#172638", 700)
    icon("region", 1084, 478, 36)
    text(1132, 504, "北弗吉尼亚 us-east-1 / us-east-1d", 22, "#172638", 700)

    box(82, 535, 922, 307, "#fffaf5", "#ed7100", 8)
    icon("ec2", 100, 550, 44)
    text(160, 575, "EC2 A · g7e.12xlarge · 2 GPU", 23, "#172638", 700)
    text(650, 575, "压测、代理与两个副本同机部署", 18, "#6a573e")
    box(104, 625, 205, 82)
    text(119, 655, "压测客户端", 21, "#172638", 700)
    text(119, 686, "完整 JSON · 100 RPS", 17, "#526174")
    box(352, 625, 254, 82, "#eff5ff", "#2764a5")
    text(368, 655, "路由 + 复核校准", 21, "#172638", 700)
    text(368, 686, "127.0.0.1:18770", 18, "#526174")
    line("M309 666 H348")
    for replica, y, port in ((0, 621, 18500), (1, 728, 18501)):
        box(677, y, 303, 82)
        text(693, y + 31, f"GPU {replica} · vLLM 副本 {replica}", 21, "#172638", 700)
        text(693, y + 61, f"完整 Karpo 4B FP8 · :{port}", 17, "#526174")
    line("M606 666 H673")
    line("M640 666 V769 H673")
    text(105, 757, "请求由一个副本完成，沿原路返回。", 18, "#526174")
    text(105, 787, "复核阈值：0.162826（原始 allow）", 18, "#526174")
    text(105, 817, "保留 answers，另返回 decision。", 18, "#526174")

    def peer(x, y, w, letter, replica, node_name, address):
        box(x, y, w, 181, "#fffaf5", "#ed7100", 8)
        icon("ec2", x + 17, y + 16, 42)
        text(x + 73, y + 44, f"EC2 {letter} · g7e.2xlarge · 1 GPU", 21, "#172638", 700)
        text(x + 18, y + 83, f"GPU {replica} · vLLM 副本 {replica} · Karpo 4B FP8", 20, "#172638", 600)
        text(x + 18, y + 117, address, 17, "#526174")
        text(x + 18, y + 150, f"部署节点：{node_name}  |  最大 batch = 4", 17, "#526174")

    peer(82, 915, 445, "B", 2, "peer2", "私网 HTTP · 172.31.42.209:18500")
    peer(559, 915, 445, "C", 3, "peer3", "私网 HTTP · 172.31.37.239:18500")
    line("M479 707 V873 H304 V911")
    line("M479 873 H782 V911")
    text(101, 897, "同可用区私网 HTTP", 17, "#2764a5")
    text(582, 897, "同可用区私网 HTTP", 17, "#2764a5")

    peer(1096, 629, 498, "D", 4, "peer4", "SSH 18504 → 远端 127.0.0.1:18500")
    peer(1096, 915, 498, "E", 5, "peer5", "SSH 18505 → 远端 127.0.0.1:18500")
    line("M479 625 V604 H1050 V706 H1092", remote=True)
    line("M1050 706 V992 H1092", remote=True)
    text(1099, 550, "跨区域 SSH 隧道 · TCP 22", 21, "#b85b08", 700)
    text(1099, 581, "仅允许 EC2 A 公网 IP /32 访问", 18, "#6a573e")
    text(1099, 850, "两张远端卡各使用一条 SSH 隧道。", 18, "#526174")
    text(1099, 881, "俄亥俄配额及容量不足，新增卡部署于此。", 18, "#526174")
    line("M58 1155 H103", arrow=False)
    text(114, 1162, "本机 / 私网 HTTP", 17, "#526174")
    line("M348 1155 H393", remote=True, arrow=False)
    text(404, 1162, "跨区域 SSH", 17, "#526174")
    text(704, 1162, "所有模型副本：vLLM 0.34.0 · FP8 · 最大 batch 4 · CUDA Graph powers", 18, "#526174")
    text(40, 1225, "计时范围：预定到达到完整 HTTP 响应，包含排队及跨区域通信；生产入口和在线特征查询未纳入。", 20, "#263549")
    text(40, 1262, "生命周期：入口代理随压测结束退出；测试结束时 5 台 EC2 保留运行，模型与隧道进程设有 6 小时上限。", 19, "#526174")
    text(40, 1308, "AWS 官方架构图标 · 2026-07-31 图标包  |  依据：promo-rtx6-20261121 已核验部署及请求记录", 16, "#667386")
    parts.append("</g></svg>\n")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true", help="Also export PNG; requires CairoSVG.")
    args = parser.parse_args()
    target = ASSETS / "promo-rtx6-aws-architecture.svg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render())
    if args.png:
        import cairosvg
        cairosvg.svg2png(url=str(target), write_to=str(target.with_suffix(".png")), scale=2)
    print(target.relative_to(ROOT))


if __name__ == "__main__":
    main()
