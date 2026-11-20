"""Freeze the existing English full-200 requests and a Karpo family comparison."""
import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/family-20261120"
PREVIOUS = ROOT / "experiments/concise-fp8-20261119"
REVISION = "6600011eb5cf46232d056ec0717dc726146d29de"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    target = EXP / "manifest.json"
    if target.exists():
        raise RuntimeError("This experiment is frozen; create a new experiment for changed inputs.")
    original = json.loads((PREVIOUS / "fixtures/manifest.json").read_text())
    for name, expected in original["files"].items():
        assert sha(PREVIOUS / name) == expected, name
    items = [r for r in json.loads((PREVIOUS / "fixtures/requests.json").read_text())
             if r["description"] == "concise-en"]
    assert len(items) == 104 and len({r["id"] for r in items}) == 104
    assert all(len(r["request"]["questions"]["action"]["criteria"]) == 200 for r in items)
    (EXP / "requests.json").write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    models = [{
        "size": "31B", "id": "MachinePulseAI/karpo-decision-31B",
        "revision": "b8e6fd7463cea8662c556d6257955d7ef9c10d19",
        "weights_bytes": 62551812872, "family": "gemma",
    }]
    for size in ["12B", "9B", "4B"]:
        meta = json.loads((ROOT / f".state/family-20261120/model-{size}.json").read_text())
        models.append({
            "size": size, "id": meta["id"], "revision": meta["sha"],
            "weights_bytes": sum(f["bytes"] for f in meta["files"] if f["path"].endswith(".safetensors")),
            "family": "gemma" if size == "12B" else "qwen",
        })
    configs = []
    for model in models:
        for precision in (["fp8"] if model["size"] == "31B" else ["fp8", "bf16"]):
            flags = ["--graphs"]
            if precision == "fp8":
                flags.append("--fp8")
            if model["family"] == "gemma":
                flags.append("--triton-attention")
            configs.append({
                "id": model["size"].lower() + "-" + precision,
                "model": model["size"], "precision": precision, "flags": flags,
                "concurrency": [1, 2, 4, 8] if precision == "fp8" else [],
            })
    files = [
        "scripts/family_prepare.py", "scripts/family_benchmark.py", "scripts/family_suite.py",
        "scripts/benchmark.py", "scripts/concise_suite.py",
        "experiments/family-20261120/PLAN.zh.md", "experiments/family-20261120/requests.json",
    ]
    manifest = {
        "frozen_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "code_revision": REVISION, "models": models, "configs": configs,
        "max_length": 8192, "warmup": 8, "quality_existing": 80, "quality_boundary": 24,
        "concurrency_requests": 24, "expected_calls": 1168,
        "source_requests_sha256": sha(PREVIOUS / "fixtures/requests.json"),
        "source_manifest_sha256": sha(PREVIOUS / "fixtures/manifest.json"),
        "files": {name: sha(ROOT / name) for name in files},
    }
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"configs": len(configs), "requests": len(items),
                      "expected_calls": manifest["expected_calls"], "manifest_sha256": sha(target)}))


if __name__ == "__main__":
    main()
