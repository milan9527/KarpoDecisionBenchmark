"""Freeze the H200-only SOP experiment without changing existing result snapshots."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-sop-h200-20261120"
SOURCE = ROOT / "experiments/vllm-rps100-20261120"


def main(revision):
    previous = json.loads((SOURCE / "manifest-fleet-v1.json").read_text())
    paths = []
    for name in ("risk-requests.json", "sop-requests.json", "encoding-reference.json"):
        original, target = SOURCE / name, EXP / name
        assert hashlib.sha256(original.read_bytes()).hexdigest() == previous["files"][str(original.relative_to(ROOT))]
        if target.exists():
            assert target.read_bytes() == original.read_bytes()
        else:
            shutil.copyfile(original, target)
        paths.append(target)
    paths += [EXP / "PLAN.zh.md"]
    paths += [ROOT / "scripts" / name for name in (
        "sop_h200_suite.py", "sop_h200_prepare.py", "vllm_rps_server.py", "vllm_rps_proxy.py",
        "vllm_rps_benchmark.py", "vllm_rps_suite.py", "vllm_karpo_encoding.py",
        "vllm_single_environment.py", "benchmark.py", "vllm_single_benchmark.py", "batch100_benchmark.py",
    )]
    paths += sorted((ROOT / "serving/vllm_karpo").glob("*.py"))
    paths += [ROOT / "serving/vllm_karpo/pyproject.toml",
              ROOT / "tests/test_vllm_rps.py", ROOT / "tests/test_vllm_karpo.py"]
    manifest = {
        "revision": revision, "scenario": "sop", "gpu_model": "H200",
        "model": previous["model"], "model_revision": previous["model_revision"],
        "code_revision": previous["code_revision"], "arrival_rps": 100, "p50_ms": 500,
        "batches": [1, 4], "replicas": [1, 2, 4, 8], "ec2_policy": "keep_running",
        "files": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    }
    target = EXP / f"manifest-{revision}.json"
    if target.exists():
        assert json.loads(target.read_text()) == manifest, "Use a new revision for changed source"
    else:
        target.write_text(json.dumps(manifest, indent=2) + "\n")
    for path in paths:
        frozen = EXP / "frozen" / revision / path.relative_to(ROOT)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        if frozen.exists():
            assert frozen.read_bytes() == path.read_bytes()
        else:
            shutil.copyfile(path, frozen)
    bundle = ROOT / ".state" / f"sop-h200-{revision}.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for path in paths + [target]:
            archive.add(path, arcname=str(path.relative_to(ROOT)))
    print(json.dumps({"path": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default="v1")
    main(parser.parse_args().revision)
