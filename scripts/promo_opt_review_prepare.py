"""Prompt-only variants plus fresh entity-disjoint synthetic quality fixtures."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tarfile

from promo_opt_prepare import EXP, ROOT, freeze_dataset, sha
from promo_risk import (INSTRUCTIONS, PRIORITY, REQUIRED, RULES, SCENARIOS, criteria,
                       digest, extract, flattened, make_case)
from promo_risk_prepare import validate_snapshot

VARIANTS = ("baseline", "split", "prose", "guard", "flat")
OP = {"eq": "is", "ge": "is at least", "le": "is at most", "gt": "is greater than", "lt": "is less than"}
MISSING = ("At least one input field used in the numeric or boolean checks for deny, deduplicate, "
           "throttle, challenge, or review is null (unknown).")
BUSINESS = {
    "refund_arbitrage": "Repeated refunds followed by renewed reward claims",
    "referral_farm": "Many referrals sharing payment accounts with few purchases",
    "reward_pooling": "Rewards concentrated into a shared payee",
    "synchronized_cluster": "A large cluster of new accounts claiming in synchrony",
    "circular_funds": "Repeated circular fund transfers",
    "proxy_rotation": "Rotating proxies across campaigns for heavily discounted orders",
    "history_shift": "A substantial increase in both requests and subsidies over established history",
}


def variants(name):
    if name == "baseline":
        return INSTRUCTIONS, criteria()
    choices = criteria()
    for action in PRIORITY[:-1]:
        clauses = []
        for rule in RULES:
            if rule["action"] != action:
                continue
            if name == "split":
                from promo_risk import OP_TEXT
                clause = " AND ".join(f'{c["feature"]} {OP_TEXT[c["op"]]} {json.dumps(c["value"])}'
                                      for c in rule["all"])
            else:
                clause = "; AND ".join(f'{c["feature"]} {OP[c["op"]]} {json.dumps(c["value"])}'
                                        for c in rule["all"])
            title = BUSINESS.get(rule["id"])
            clauses.append((title + ": " if title and name != "split" else "") + clause + ".")
        if action == "review":
            clauses.append(MISSING)
        choices[action] = ("Choose this action if ANY ONE of these checks matches. Within each check, ALL "
                           "conditions must hold:\n" + "\n".join(f"{i}. {clause}" for i, clause in enumerate(clauses, 1)))
    choices["allow"] = ("Allow only when none of the deny, deduplicate, throttle, challenge, or review checks "
                        "matches and no field referenced in those checks is null.")
    instructions = INSTRUCTIONS + (
        " Evaluate the explicit checks even for old or verified accounts: reassuring context never cancels "
        "a matching rule. A review check routes the request for investigation; proof of fraud is not required. "
        "Choose allow only after ruling out every applicable review check."
    )
    if name == "guard":
        # Boolean complement of each review conjunction; generated from identical thresholds.
        complements = {"eq": lambda c: f'{c["feature"]} is not {json.dumps(c["value"])}',
                       "ge": lambda c: f'{c["feature"]} is less than {json.dumps(c["value"])}',
                       "le": lambda c: f'{c["feature"]} is greater than {json.dumps(c["value"])}'}
        exclusions = ["(" + " OR ".join(complements[c["op"]](c) for c in rule["all"]) + ")"
                      for rule in RULES if rule["action"] == "review"]
        choices["allow"] += (" In particular, ALL seven exclusions must hold:\n" +
                             "\n".join(f"{i}. {clause}." for i, clause in enumerate(exclusions, 1)))
    return instructions, choices


def transform(row, name):
    row = copy.deepcopy(row)
    original = copy.deepcopy(row["request"]["state"]["trusted_features"])
    instruction, choices = variants(name)
    if row["order"] == "reversed":
        choices = dict(reversed(list(choices.items())))
    row["request"]["questions"]["action"].update(instructions=instruction, criteria=choices)
    if name == "flat":
        row["request"]["state"]["trusted_features"] = flattened(original)
    actual = row["request"]["state"]["trusted_features"]
    assert (actual if name == "flat" else flattened(actual)) == flattened(original)
    assert len(flattened(original)) == 200
    row["request_sha256"] = digest(row["request"])
    return row


def main():
    target = EXP / "review-manifest-v1.json"
    if target.exists():
        raise RuntimeError("Review fixtures already frozen")
    existing = json.loads((EXP / "inputs/original/risk-requests.json").read_text())
    tune = [row for row in existing if row["group"] == "tune"]
    heldout, snapshots = [], []
    for scenario, title, expected, category in SCENARIOS:
        for index in range(12):
            snapshot, rows, schema = make_case(scenario, title, expected, category, "review-holdout-v1", index)
            features, _ = extract(snapshot)
            validate_snapshot(snapshot, features, schema)
            snapshots.append(snapshot)
            for row in rows:
                row["group"] = "quality"
            heldout.extend(rows)
    old_ids = {r["case_id"] for r in existing}
    assert not old_ids & {r["case_id"] for r in heldout}
    assert not {r["request_sha256"] for r in existing} & {r["request_sha256"] for r in heldout}
    source = EXP / "review-source-events-v1.json.gz"
    with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as stream:
        stream.write(json.dumps(snapshots, ensure_ascii=False, separators=(",", ":")).encode())
    files = [source, Path(__file__), ROOT / "scripts/promo_risk_prepare.py"]
    union = []
    for name in VARIANTS:
        directory = EXP / "inputs" / ("review-" + name)
        directory.mkdir(parents=True, exist_ok=False)
        rows = [transform(row, name) for row in tune + heldout]
        (directory / "risk-requests.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        (directory / "sop-requests.json").write_text("[]\n")
        files += freeze_dataset(directory)
        # The union controls Graph shapes only; its IDs are namespaced to remain unambiguous.
        for row in rows:
            other = copy.deepcopy(row)
            other["id"] = name + "-" + other["id"]
            union.append(other)
    directory = EXP / "inputs/review-union"
    directory.mkdir()
    (directory / "risk-requests.json").write_text(json.dumps(union, ensure_ascii=False, indent=2) + "\n")
    (directory / "sop-requests.json").write_text("[]\n")
    files += freeze_dataset(directory)
    manifest = {
        "variants": list(VARIANTS), "tune_requests_per_variant": len(tune),
        "fresh_quality_requests_per_variant": len(heldout),
        "shared_scenario_templates": True, "production_accuracy_claim": False,
        "quality_seed_namespace": "review-holdout-v1", "entity_and_request_disjoint_from_previous": True,
        "selection": "Highest tune accuracy, then review recall, then shortest average JSON; baseline eligible. "
                     "No quality predictions before selection is frozen.",
        "preserved": "All 200 feature values, six choices, condition thresholds and action priority; flat changes key layout only.",
        "required_features": list(REQUIRED),
        "files": {str(p.relative_to(ROOT)): sha(p) for p in files},
    }
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    bundle = ROOT / ".state/promo-opt-review-v1.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for path in files + [target]:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    print(json.dumps({"bundle": str(bundle), "sha256": sha(bundle), "bytes": bundle.stat().st_size,
                      "heldout": len(heldout), "variants": len(VARIANTS)}))


if __name__ == "__main__":
    main()
