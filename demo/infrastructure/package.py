"""Build a versioned runtime using the existing model code and original inputs."""
import hashlib
import json
from pathlib import Path
import tarfile
from datasets import build

ROOT = Path(__file__).resolve().parents[2]


def main():
    build()
    risk_dir = ROOT / "experiments/promo-rtx6-20261121/inputs/review-baseline"
    sop_dir = ROOT / "experiments/vllm-sop-h200-20261120"
    risk = json.loads((risk_dir / "risk-requests.json").read_text())
    sop = json.loads((sop_dir / "sop-requests.json").read_text())
    choices = [
        ("refund_arbitrage", "退款后重复领权益", "10笔订单中8笔退款，观察复核校准"),
        ("normal", "正常用户请求", "正常账户与设备，查看放行动作"),
        ("signature_tamper", "请求签名异常", "验证无效签名的拒绝策略"),
    ]
    risk_examples = []
    for behavior, title, detail in choices:
        row = next(r for r in risk if r["behavior"] == behavior and r["id"].endswith("original")
                   and (behavior != "refund_arbitrage" or r["id"].startswith("review-holdout-v1")))
        risk_examples.append({"id": row["id"], "title": title, "detail": detail, "request": row["request"]})
    sop_examples = []
    for case, title, detail in (
        ("boundary-duplicate-over-approved", "重复提现请求", "操作已经执行，用户要求再次执行"),
        ("boundary-hold-over-approved", "审批通过但存在冻结", "合规冻结优先于审批与客户要求"),
        ("boundary-destination-before-execution", "新提现地址未验证", "根据可信前置条件选择下一步"),
    ):
        row = next(r for r in sop if r["id"] == f"concise-en:{case}-original")
        sop_examples.append({"id": row["id"], "title": title, "detail": detail, "request": row["request"]})
    catalog = {"risk": {"examples": risk_examples}, "sop": {"examples": sop_examples}}
    (ROOT / "demo/catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    paths = list((ROOT / "demo/backend").glob("*.py")) + [
        ROOT / "demo/backend/requirements.txt", ROOT / "demo/config.json", ROOT / "demo/catalog.json", ROOT / "demo/datasets.json",
        ROOT / "scripts/promo_opt_server.py", ROOT / "scripts/vllm_rps_server.py",
        ROOT / "scripts/vllm_karpo_encoding.py",
    ]
    paths += list((ROOT / "serving/vllm_karpo").glob("*.py"))
    paths += [directory / filename for directory in (risk_dir, sop_dir) for filename in ("risk-requests.json", "sop-requests.json")]
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    target = ROOT / ".state" / f"demo-runtime-{digest}.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    info = {"archive": str(target), "key": f"runtime/{target.name}", "files": manifest,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    (ROOT / "operations/demo-20261121/runtime.json").write_text(json.dumps(info, indent=2) + "\n")
    print("Packaged", len(paths), "files", target.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
