"""Review escalation from real model scores; raw Karpo answers stay unchanged."""
import copy
import hashlib
import json
import math
from pathlib import Path

from promo_risk_evaluate import evaluate_responses

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-opt-20261121"


def decide(answer, threshold):
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid review threshold")
    p = answer["probabilities"]
    for value in p.values():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Invalid model probability")
    total = p["review"] + p["allow"]
    score = p["review"] / total if total else 0.0
    raw = answer["choice"]
    chosen = "review" if raw == "allow" and score >= threshold else raw
    return {"action": chosen, "raw_action": raw, "review_pair_score": score,
            "threshold": threshold, "escalated": chosen != raw, "policy": "review-threshold-v1"}


def load_calibration():
    path = EXP / "review-calibration-v1.json"
    value = json.loads(path.read_text())
    return value["selected"]["threshold"], hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(fixtures, rows):
    calibrated = copy.deepcopy(rows)
    for row in calibrated:
        if row.get("valid"):
            row["response"]["answers"]["action"]["choice"] = row["response"]["decision"]["action"]
    result = evaluate_responses(fixtures, calibrated)
    result["scope"] = "Calibrated review decision on actual model probabilities; raw model answers retained separately"
    return result


def verify_fit():
    """Reproduce the frozen tune-only fit; do not overwrite its original time."""
    value = json.loads((EXP / "review-calibration-v1.json").read_text())
    source = ROOT / value["source"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == value["source_sha256"]
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    fixtures = {r["id"]: r for r in json.loads(
        (EXP / "inputs/review-baseline/risk-requests.json").read_text()) if r["group"] == "tune"}
    assert len(rows) == len(fixtures) == 360 and all(r["valid"] for r in rows)
    scores = sorted({decide(r["response"]["answers"]["action"], 1)["review_pair_score"]
                     for r in rows if r["response"]["answers"]["action"]["choice"] == "allow"})
    thresholds = [0.0, 1.0] + [(a + b) / 2 for a, b in zip(scores, scores[1:])]
    results = []
    for threshold in thresholds:
        correct = review_correct = review_attempts = benign_interventions = benign_attempts = abuse_allowed = 0
        for row in rows:
            fixture = fixtures[row["request_id"]]
            choice = decide(row["response"]["answers"]["action"], threshold)["action"]
            expected = row["expected"]["action"]
            correct += choice == expected
            review_attempts += expected == "review"
            review_correct += expected == "review" and choice == "review"
            benign_attempts += fixture["category"] == "benign"
            benign_interventions += fixture["category"] == "benign" and choice in {"deny", "throttle", "challenge", "review"}
            abuse_allowed += fixture["category"] == "abuse" and choice == "allow"
        results.append({"threshold": threshold, "correct": correct, "attempts": len(rows),
                        "accuracy": correct / len(rows), "review_correct": review_correct,
                        "review_attempts": review_attempts, "benign_interventions": benign_interventions,
                        "benign_attempts": benign_attempts, "abuse_allowed": abuse_allowed})
    assert results == value["candidates"]
    eligible = [r for r in results if r["benign_interventions"] / r["benign_attempts"] <= .01]
    best = sorted(eligible, key=lambda r: (-r["accuracy"], r["abuse_allowed"],
                                          r["benign_interventions"], -r["threshold"]))[0]
    assert best == value["selected"]
    return best


if __name__ == "__main__":
    print(json.dumps(verify_fit(), indent=2))
