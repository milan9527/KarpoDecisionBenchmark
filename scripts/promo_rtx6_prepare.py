"""Freeze reused inputs, calibration, six-node topology and exact runtime sources."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-rtx6-20261121"
PREVIOUS = ROOT / "experiments/promo-opt-20261121"


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def dump(p, v):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(v, ensure_ascii=False, indent=2) + "\n")


def main():
    assert not (EXP / "manifest-v1.json").exists(), "Frozen experiment already exists"
    data = EXP / "inputs/review-baseline"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("risk-requests.json", "sop-requests.json"):
        shutil.copyfile(PREVIOUS / "inputs/review-baseline" / name, data / name)
    shutil.copyfile(PREVIOUS / "results/encodings/review-baseline.json", data / "expected-encoding.json")
    dump(data / "manifest.json", {"files": {p.name: sha(p) for p in sorted(data.glob("*.json"))
                                          if p.name != "manifest.json"}})
    shutil.copyfile(PREVIOUS / "review-calibration-v1.json", EXP / "review-calibration-v1.json")
    hosts = [
        {"name": "dual", "instance_id": "i-05b24e3b4a7ee864d", "region": "us-east-2", "az": "us-east-2c",
         "instance_type": "g7e.12xlarge", "replica_ids": [0, 1], "ssh_address": None,
         "new": False, "cross_region": False, "market": "spot",
         "previous_unit": "karpo-opt-review-selected-dual"},
    ]
    for p in json.loads((ROOT / "experiments/promo-risk-rtx4-20261121/operations/small-peers.json").read_text()):
        hosts.append({"name": "peer"+str(p["replica_id"]), "instance_id": p["instance_id"],
            "instance_type": p["instance_type"], "region": p["region"], "az": p["az"],
            "replica_ids": [p["replica_id"]], "ssh_address": p["private_ip"], "new": False,
            "cross_region": False, "market": p["market"],
            "previous_unit": "karpo-opt-review-selected-peer"+str(p["replica_id"])})
    for p in json.loads((EXP / "operations/new-peers.json").read_text()):
        hosts.append({"name": "peer"+str(p["replica_id"]), "instance_id": p["instance_id"],
            "instance_type": p["instance_type"], "region": p["region"], "az": p["az"],
            "replica_ids": p.get("replica_ids", [p["replica_id"]]),
            "ssh_address": p["public_ip"] if p["region"] != "us-east-2" else p["private_ip"],
            "new": True, "cross_region": p["region"] != "us-east-2", "market": p["market"]})
    assert sorted(i for h in hosts for i in h["replica_ids"]) == list(range(6))
    urls = ["http://127.0.0.1:18500", "http://127.0.0.1:18501",
            "http://172.31.42.209:18500", "http://172.31.37.239:18500",
            "http://127.0.0.1:18504", "http://127.0.0.1:18505"]
    weights = json.loads((ROOT / "experiments/promo-risk-rtx4-20261121/results/setup/weights-ip-172-31-40-12.json").read_text())
    dump(EXP / "fleet-v1.json", {"hosts": hosts, "urls": urls, "expected_weights": weights["weights"],
         "network": "Four Ohio GPUs; two Virginia GPUs reached through loopback-bound SSH tunnels"})
    dump(EXP / "operations/provenance.json", {
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "previous_archive": json.loads((PREVIOUS / "operations/archive-receipt.json").read_text()),
        "risk_requests_sha256": sha(data / "risk-requests.json"),
        "expected_encoding_sha256": sha(data / "expected-encoding.json"),
        "calibration_sha256": sha(EXP / "review-calibration-v1.json"),
        "quality_scope": "Reuse previous heldout requests for hardware scaling validation; not a new holdout"})
    files = [
        ROOT / "scripts" / name for name in (
            "promo_rtx6_runtime.py", "promo_rtx6_controller.py", "promo_rtx6_prepare.py",
            "promo_opt_server.py", "promo_opt_calibrated_proxy.py", "promo_opt_calibrated_benchmark.py",
            "promo_opt_calibration.py", "promo_risk_evaluate.py", "promo_risk.py",
            "vllm_rps_benchmark.py", "vllm_rps_proxy.py", "vllm_karpo_encoding.py",
            "vllm_single_environment.py", "benchmark.py")]
    files += list((ROOT / "serving/vllm_karpo").glob("karpo_vllm*.py"))
    files += [ROOT / "serving/vllm_karpo/pyproject.toml",
              PREVIOUS / "review-calibration-v1.json", EXP / "PLAN.zh.md",
              EXP / "fleet-v1.json", EXP / "review-calibration-v1.json",
              ROOT / "tests/test_promo_rtx6.py", *sorted(data.glob("*.json"))]
    manifest = {"frozen_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "revision": "v1",
        "gpu_count": 6, "batch": 4, "graph": "powers", "calibration": "unchanged review-threshold-v1",
        "files": {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)}}
    dump(EXP / "manifest-v1.json", manifest)
    archive = ROOT / ".state/promo-rtx6-v1.tar.gz"
    archive.parent.mkdir(exist_ok=True)
    with tarfile.open(archive, "w:gz") as output:
        for p in sorted([*files, EXP / "manifest-v1.json", EXP / "operations/provenance.json"]):
            output.add(p, arcname=str(p.relative_to(ROOT)), recursive=False)
    dump(EXP / "bundle-v1.json", {"sha256": sha(archive), "bytes": archive.stat().st_size,
                                 "remote_archive": "/tmp/promo-rtx6-v1.tar.gz"})
    print((EXP / "bundle-v1.json").read_text())


if __name__ == "__main__":
    main()
