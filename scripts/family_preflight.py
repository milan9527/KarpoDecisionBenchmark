"""Check the frozen family experiment without loading a model or starting a GPU."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

from prepare import digest

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/family-20261120"


def main():
    manifest = json.loads((EXP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    source = ROOT / "experiments/concise-fp8-20261119/fixtures/requests.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest["source_requests_sha256"]
    items = json.loads((EXP / "requests.json").read_text())
    assert items == [r for r in json.loads(source.read_text()) if r["description"] == "concise-en"]
    assert len(items) == 104 and len({r["case_id"] for r in items}) == 52
    for item in items:
        assert digest(item["request"]) == item["request_sha256"], item["id"]
        criteria = item["request"]["questions"]["action"]["criteria"]
        assert len(criteria) == item["candidate_count"] == 200
        assert item["expected"]["action"] in criteria
        assert "expected" not in item["request"]
    models = {m["size"]: m for m in manifest["models"]}
    assert set(models) == {"31B", "12B", "9B", "4B"}
    assert all(re.fullmatch("[0-9a-f]{40}", m["revision"]) for m in models.values())
    expected = 0
    for config in manifest["configs"]:
        assert (config["precision"] == "fp8") == ("--fp8" in config["flags"])
        assert "--graphs" in config["flags"]
        assert ("--triton-attention" in config["flags"]) == (models[config["model"]]["family"] == "gemma")
        expected += 8 + 80 + 24 + len(config["concurrency"]) * manifest["concurrency_requests"]
    assert len(manifest["configs"]) == 7 and expected == manifest["expected_calls"] == 1168
    result = {
        "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "passed", "gpu_used": False, "model_calls": 0,
        "manifest_sha256": hashlib.sha256((EXP / "manifest.json").read_bytes()).hexdigest(),
        "checks": [
            "all frozen execution and input file hashes match",
            "104 requests exactly equal the previous English concise requests",
            "52 distinct synthetic cases, each in two candidate orders",
            "every request contains all 200 candidates and the expected ID is present",
            "four pinned model revisions and seven valid backend/precision configurations",
            "1168 planned calls including warmup and repeated concurrency requests",
        ],
        "not_checked": ["GPU runtime compatibility", "tokenized lengths for each model", "quality and latency"],
    }
    (EXP / "results").mkdir(exist_ok=True)
    (EXP / "results/preflight.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
