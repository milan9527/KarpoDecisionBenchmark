"""Freeze byte-identical inputs and executable sources before GPU execution."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/smallbatch3-retry-20261120"
PREVIOUS = ROOT / "experiments/batch100-20261120"


def main():
    assert not (EXP / "manifest.json").exists(), "Already frozen"
    previous = json.loads((PREVIOUS / "manifest.json").read_text())
    paths = []
    for name in ("risk-requests.json", "sop-requests.json", "risk-feature-schema.json"):
        source = PREVIOUS / name
        expected = previous["files"][str(source.relative_to(ROOT))]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
        shutil.copyfile(source, EXP / name)
        paths.extend([source, EXP / name])
    for scenario in ("risk", "sop"):
        paths.append(PREVIOUS / f"results/fp8/{scenario}/quality-serial/requests.jsonl")
    paths.extend(ROOT / "scripts" / name for name in (
        "smallbatch3_retry_prepare.py", "smallbatch_server.py", "smallbatch3_proxy.py", "smallbatch_proxy.py",
        "smallbatch3_retry_benchmark.py", "smallbatch3_retry_suite.py", "batch100_server.py",
        "batch100_benchmark.py", "benchmark.py", "concise_suite.py"))
    paths.append(EXP / "PLAN.zh.md")
    manifest = {
        "experiment": EXP.name, "model": previous["model"],
        "model_revision": previous["model_revision"], "code_revision": previous["code_revision"],
        "precision": "FP8", "batch_sizes": [1, 2, 4], "wait_ms": [0], "replicas": [1, 2, 3],
        "concurrency": 100, "sustained_seconds": 60, "max_length": 8192, "token_budget": 65536,
        "selection": "Three replicas: no performance/quality failures, synthetic quality >90%, lowest sustained P50; tie higher valid RPS. Repeat once.",
        "files": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in paths},
    }
    (EXP / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    paths.append(EXP / "manifest.json")
    bundle = ROOT / ".state/smallbatch3-retry-inputs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=str(path.relative_to(ROOT)))
    print(json.dumps({"bundle": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                      "files": len(paths)}))


if __name__ == "__main__":
    main()
