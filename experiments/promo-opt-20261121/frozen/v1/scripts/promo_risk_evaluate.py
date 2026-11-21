"""Evaluate one new quality pass; never import the old experiment's metrics."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

from promo_risk import PRIORITY, digest


def evaluate_responses(fixtures, responses):
    quality = {row["id"]: row for row in fixtures if row["group"] == "quality"}
    if not quality:
        raise ValueError("no quality fixtures")
    actual = {}
    for row in responses:
        key = row.get("request_id")
        if key not in quality:
            raise ValueError(f"unknown/non-quality request: {key}")
        if key in actual:
            raise ValueError(f"duplicate response ID: {key}; evaluate each quality pass separately")
        expected_hash = digest(quality[key]["request"])
        if quality[key]["request_sha256"] != expected_hash or row.get("request_sha256") != expected_hash:
            raise ValueError(f"input hash mismatch: {key}")
        actual[key] = row
    groups = {name: defaultdict(lambda: {"attempts": 0, "valid": 0, "correct": 0, "failures": 0})
              for name in ("action", "behavior", "category")}
    observations, pairs = [], defaultdict(dict)
    benign_interventions = benign_count = abuse_allow = abuse_count = 0
    latencies = []
    for key, fixture in quality.items():
        row = actual.get(key)
        choice = None
        if row is not None:
            try:
                choice = row["response"]["answers"]["action"]["choice"]
            except (KeyError, TypeError):
                pass
        latency = row.get("latency_ms") if row else None
        valid = bool(row and row.get("valid") is True and row.get("http_status") == 200 and choice in PRIORITY and
                     isinstance(latency, (int, float)) and not isinstance(latency, bool) and
                     math.isfinite(latency) and latency >= 0)
        correct = valid and choice == fixture["expected"]["action"]
        for group, value in (("action", fixture["expected"]["action"]),
                             ("behavior", fixture["behavior"]), ("category", fixture["category"])):
            counter = groups[group][value]
            counter["attempts"] += 1
            counter["valid"] += valid
            counter["correct"] += correct
            counter["failures"] += not valid
        if fixture["category"] == "benign":
            benign_count += 1
            benign_interventions += valid and choice in {"deny", "throttle", "challenge", "review"}
        if fixture["category"] == "abuse":
            abuse_count += 1
            abuse_allow += valid and choice == "allow"
        if valid:
            latencies.append(latency)
        pairs[fixture["case_id"]][fixture["order"]] = choice if valid else None
        observations.append({"id": key, "expected": fixture["expected"]["action"],
                             "choice": choice, "valid": valid, "correct": correct})
    total = len(observations)
    valid_count = sum(r["valid"] for r in observations)
    correct_count = sum(r["correct"] for r in observations)
    paired_valid = [p for p in pairs.values() if p.get("original") is not None and p.get("reversed") is not None]
    percentile = lambda p: sorted(latencies)[max(0, math.ceil(p*len(latencies))-1)] if latencies else None
    def rate(a, b):
        return a / b if b else None
    return {
        "scope": "one synthetic quality pass; harness valid=true must include fresh encoding checks; not a sustained 100 RPS benchmark",
        "attempts": total, "responses_received": len(actual), "missing_responses": total-len(actual),
        "valid": valid_count, "failures": total-valid_count, "correct": correct_count,
        "accuracy_all_attempts": rate(correct_count, total),
        "accuracy_valid_responses": rate(correct_count, valid_count),
        "benign_interventions": benign_interventions, "benign_attempts": benign_count,
        "benign_intervention_rate_all_attempts": rate(benign_interventions, benign_count),
        "abuse_allowed": abuse_allow, "abuse_attempts": abuse_count,
        "abuse_allow_rate_all_attempts": rate(abuse_allow, abuse_count),
        "rate_note": "Failed/missing requests remain failures, not assumed safe or allowed; consult group failures alongside rates.",
        "order_pairs": len(pairs), "valid_order_pairs": len(paired_valid),
        "order_disagreements": sum(p["original"] != p["reversed"] for p in paired_valid),
        "valid_latency_p50_ms": percentile(0.5), "valid_latency_p95_ms": percentile(0.95),
        "groups": {key: dict(value) for key, value in groups.items()},
        "errors": [r for r in observations if not r["correct"]],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixtures = json.loads(args.fixtures.read_text())
    responses = [json.loads(line) for line in args.responses.read_text().splitlines() if line.strip()]
    result = evaluate_responses(fixtures, responses)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("groups", "errors")}, ensure_ascii=False, indent=2))
