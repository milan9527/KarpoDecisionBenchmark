"""Freeze inputs and adapter before a GPU trial; never modify old experiments."""

import hashlib
import json
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-single-20261120"


def main():
    source = ROOT / "experiments/batch100-20261120"
    previous = json.loads((source / "manifest.json").read_text())
    paths = []
    for name in ["risk-requests.json", "sop-requests.json", "risk-feature-schema.json"]:
        original = source / name
        expected = previous["files"][str(original.relative_to(ROOT))]
        assert hashlib.sha256(original.read_bytes()).hexdigest() == expected
        if (EXP / name).exists():
            assert (EXP / name).read_bytes() == original.read_bytes()
        else:
            shutil.copyfile(original, EXP / name)
        paths.append(EXP / name)
    paths += [
        ROOT / "scripts" / name for name in (
            "vllm_karpo_encoding.py", "vllm_karpo_server.py", "vllm_single_benchmark.py",
            "vllm_single_prepare.py", "vllm_single_suite.py", "batch100_benchmark.py", "benchmark.py",
            "smallbatch_server.py", "batch100_server.py", "vllm_single_environment.py", "vllm_single_smoke.py",
        )
    ]
    paths += sorted((ROOT / "serving/vllm_karpo").glob("*.py"))
    paths += [ROOT / "serving/vllm_karpo/pyproject.toml", EXP / "PLAN.zh.md"]
    paths.append(ROOT / "tests/test_vllm_karpo.py")
    for scenario in ("risk", "sop"):
        for precision in ("fp8", "bf16"):
            paths.append(source / "results" / precision / scenario / "quality-serial/requests.jsonl")
    manifest = {
        "experiment": EXP.name, "model": previous["model"],
        "model_revision": previous["model_revision"], "code_revision": previous["code_revision"],
        "vllm_version": "0.34.0", "precision": "FP8", "replicas": 1,
        "vllm_max_num_seqs": [1, 4, 8], "concurrency": 100, "sustained_seconds": 60,
        "files": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    }
    if (EXP / "manifest.json").exists():
        assert json.loads((EXP / "manifest.json").read_text()) == manifest, "Frozen source changed"
    else:
        (EXP / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    paths.append(EXP / "manifest.json")
    bundle = ROOT / ".state/vllm-single-inputs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=str(path.relative_to(ROOT)))
    print(json.dumps({"bundle": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
