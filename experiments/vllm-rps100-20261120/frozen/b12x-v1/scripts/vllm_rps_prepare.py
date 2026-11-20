"""Versioned inputs and source snapshots; older trials remain reproducible."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"


def main(revision):
    source = ROOT / "experiments/vllm-single-20261120"
    previous = json.loads((source / "manifest.json").read_text())
    paths = []
    for name in ["risk-requests.json", "sop-requests.json", "risk-feature-schema.json"]:
        original = source / name
        assert hashlib.sha256(original.read_bytes()).hexdigest() == previous["files"][str(original.relative_to(ROOT))]
        target = EXP / name
        if target.exists():
            assert target.read_bytes() == original.read_bytes()
        else:
            shutil.copyfile(original, target)
        paths.append(target)
    encoding = EXP / "encoding-reference.json"
    if not encoding.exists():
        shutil.copyfile(source / "results/encoding/native.json", encoding)
    assert encoding.read_bytes() == (source / "results/encoding/native.json").read_bytes()
    paths += [encoding, EXP / "PLAN.zh.md"]
    paths += [ROOT / "scripts" / name for name in (
        "vllm_rps_server.py", "vllm_rps_proxy.py", "vllm_rps_benchmark.py",
        "vllm_rps_suite.py", "vllm_rps_prepare.py", "vllm_karpo_encoding.py",
        "vllm_single_environment.py", "benchmark.py",
    )]
    paths += list((ROOT / "serving/vllm_karpo").glob("*.py"))
    paths += [ROOT / "serving/vllm_karpo/pyproject.toml", ROOT / "tests/test_vllm_rps.py"]
    manifest = {
        "revision": revision, "model": previous["model"], "model_revision": previous["model_revision"],
        "code_revision": previous["code_revision"], "arrival_rps": 100, "p50_ms": 500,
        "ec2_policy": "keep_running", "files": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
    }
    path = EXP / f"manifest-{revision}.json"
    if path.exists():
        assert json.loads(path.read_text()) == manifest, "Use a new revision; do not overwrite frozen source"
    else:
        path.write_text(json.dumps(manifest, indent=2) + "\n")
    for item in paths:
        archive = EXP / "frozen" / revision / item.relative_to(ROOT)
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            assert archive.read_bytes() == item.read_bytes()
        else:
            shutil.copyfile(item, archive)
    bundle = ROOT / ".state" / f"vllm-rps-{revision}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        for item in paths + [path]:
            tar.add(item, arcname=str(item.relative_to(ROOT)))
    print(json.dumps({"path": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default="v1")
    main(parser.parse_args().revision)
