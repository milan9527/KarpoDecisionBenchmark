"""Freeze stage-one/two inputs and runtime before model measurements."""
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-opt-20261121"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_dataset(directory):
    files = sorted(p for p in directory.glob("*.json") if p.name != "manifest.json")
    manifest = {"files": {p.name: sha(p) for p in files}}
    path = directory / "manifest.json"
    if path.exists():
        assert json.loads(path.read_text()) == manifest
    else:
        path.write_text(json.dumps(manifest, indent=2) + "\n")
    return files + [path]


def main():
    files = [EXP / "PLAN.zh.md"]
    for name in ("old", "original"):
        files += freeze_dataset(EXP / "inputs" / name)
    files += [ROOT / "scripts" / name for name in (
        "promo_opt_prepare.py", "promo_opt_controller.py", "promo_opt_runtime.py", "promo_opt_server.py",
        "promo_risk.py", "promo_risk_evaluate.py", "vllm_rps_benchmark.py", "vllm_rps_proxy.py",
        "vllm_karpo_encoding.py", "vllm_single_environment.py", "benchmark.py",
    )]
    files += sorted((ROOT / "serving/vllm_karpo").glob("*.py"))
    files += [ROOT / "serving/vllm_karpo/pyproject.toml"]
    manifest = {"files": {str(p.relative_to(ROOT)): sha(p) for p in files},
                "model": "MachinePulseAI/karpo-decision-4B", "precision": "FP8 with original FP32 readout",
                "gpu": "4 x RTX PRO 6000 Blackwell 96GB, us-east-2c",
                "input_policy": "old/original bytes and real encoder outputs must equal prior experiments",
                "acceptance": {"rps": 100, "p50_ms": 500, "accuracy": .9, "formal_seconds": [120, 120]},
                "revision": "v1"}
    target = EXP / "manifest-v1.json"
    if target.exists():
        assert json.loads(target.read_text()) == manifest, "Frozen runtime changed"
    else:
        target.write_text(json.dumps(manifest, indent=2) + "\n")
    for path in files:
        dest = EXP / "frozen/v1" / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            assert dest.read_bytes() == path.read_bytes()
        else:
            dest.write_bytes(path.read_bytes())
    bundle = ROOT / ".state/promo-opt-v1.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for path in files + [target]:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    record = {"bundle": str(bundle), "sha256": sha(bundle), "bytes": bundle.stat().st_size}
    (EXP / "bundle-v1.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
