"""Freeze the actual-model harness separately from the offline dataset."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-risk-rtx4-20261121"
DATA = ROOT / "experiments/promo-risk-20261121"


def prepare(revision):
    original = json.loads((DATA / "manifest.json").read_text())
    for name, expected in original["files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == expected, name
    for name, expected in original["sources"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    EXP.mkdir(parents=True, exist_ok=True)
    request_path = EXP / "risk-requests.json"
    if not request_path.exists():
        request_path.symlink_to("../promo-risk-20261121/risk-requests.json")
    assert request_path.read_bytes() == (DATA / "risk-requests.json").read_bytes()
    sop = EXP / "sop-requests.json"
    if not sop.exists():
        sop.write_text("[]\n")
    assert json.loads(sop.read_text()) == []
    files = [request_path, sop, EXP / "PLAN.zh.md"]
    files += [DATA / name for name in ("risk-requests.json", "risk-feature-schema.json", "policy.json",
                                     "scenarios.json", "validation.json", "manifest.json")]
    files += [ROOT / "scripts" / name for name in (
        "promo_rtx4_suite.py", "promo_rtx4_prepare.py", "promo_risk.py", "promo_risk_evaluate.py",
        "promo_risk_prepare.py", "vllm_rps_benchmark.py", "vllm_rps_proxy.py", "vllm_rps_server.py",
        "vllm_karpo_encoding.py", "vllm_single_environment.py", "benchmark.py",
    )]
    files += [ROOT / "tests" / name for name in ("test_vllm_rps.py", "test_vllm_karpo.py", "test_promo_risk.py")]
    files += sorted((ROOT / "serving/vllm_karpo").glob("*.py"))
    files += [ROOT / "serving/vllm_karpo/pyproject.toml"]
    manifest = {
        "revision": revision, "dataset_manifest_sha256": hashlib.sha256((DATA / "manifest.json").read_bytes()).hexdigest(),
        "model": "MachinePulseAI/karpo-decision-4B",
        "model_revision": "f7f521cfc3219ca28518c6bba91960b8757dce06",
        "karpo_revision": "6600011eb5cf46232d056ec0717dc726146d29de",
        "gpu": "4 x RTX PRO 6000 Blackwell Server Edition 96GB",
        "precision": "FP8 per channel, original FP32 readout",
        "batch": 4, "rps": 100, "confirm_seconds": [120, 120],
        "p50_ms": 500, "accuracy": 0.9,
        "policy": "No feature pruning, no answer cache, original frozen fixtures; quality failures remain failures.",
        "files": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }
    target = EXP / f"manifest-{revision}.json"
    if target.exists():
        assert json.loads(target.read_text()) == manifest, "Changed source needs a new revision"
    else:
        target.write_text(json.dumps(manifest, indent=2) + "\n")
    for p in files:
        dest = EXP / "frozen" / revision / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            assert dest.read_bytes() == p.read_bytes()
        else:
            dest.write_bytes(p.read_bytes())
    bundle = ROOT / ".state" / f"promo-risk-rtx4-{revision}.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for p in files + [target]:
            archive.add(p, arcname=str(p.relative_to(ROOT)), recursive=False)
    record = {"bundle": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
              "bytes": bundle.stat().st_size, "revision": revision}
    (EXP / f"bundle-{revision}.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default="v1")
    print(json.dumps(prepare(parser.parse_args().revision), indent=2))
