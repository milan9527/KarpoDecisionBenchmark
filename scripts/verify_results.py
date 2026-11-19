"""Check saved measurements against frozen inputs and summaries before releasing EC2."""
import collections
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    fixtures = [*json.loads((ROOT / "fixtures/requests.json").read_text()),
                *json.loads((ROOT / "fixtures/payment.json").read_text())]
    by_id = {item["id"]: item for item in fixtures}
    outcomes = json.loads((ROOT / "results/suite-outcomes.json").read_text())["configs"]
    checks = []
    for outcome in outcomes:
        name = outcome["name"]
        if outcome["status"] != "complete":
            checks.append({"name": name, "status": outcome["status"], "verified_complete": False})
            continue
        directory = ROOT / "results" / name
        plan = json.loads((directory / "plan.json").read_text())
        for relative, expected_hash in plan["manifest"]["files"].items():
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected_hash, relative
        if plan["requested_phase"] != "all":
            raise AssertionError(f"Not a full run: {name}")
        rows = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
        summary = json.loads((directory / "summary.json").read_text())
        complete = json.loads((directory / "complete.json").read_text())
        groups = collections.defaultdict(list)
        invoked = 0
        for row in rows:
            groups[row["stage"]].append(row)
            item = by_id[row["request_id"]]
            if row.get("error") == "client_inflight_limit_32":
                continue
            invoked += 1
            assert row["request_sha256"] == item["request_sha256"], row["request_id"]
            assert row["expected"] == item["expected"], row["request_id"]
            if row["valid"]:
                actual = {k: row["response"]["answers"][k]["choice"] for k in item["expected"]}
                assert row["correct"] == (actual == item["expected"]), row["request_id"]
        assert invoked == complete["total_observations"], name
        for stage, group in groups.items():
            s = summary[stage]
            assert s["attempts"] == len(group), (name, stage)
            assert s["valid"] == sum(r["valid"] for r in group), (name, stage)
            assert s["correct"] == sum(r["correct"] for r in group), (name, stage)
        for stage, expected in [
            ("quality-existing-payment", [i for i in fixtures if i["group"] == "existing_payment"]),
            ("quality-200-detailed", [i for i in fixtures if i["group"] == "synthetic" and
                                     i["candidate_count"] == 200 and i["description"] == "detailed"]),
        ]:
            assert collections.Counter(r["request_id"] for r in groups[stage]) == \
                collections.Counter(i["id"] for i in expected), (name, stage)
        checks.append({"name": name, "verified_complete": True, "recorded_rows": len(rows),
                       "invocations": invoked, "stages": len(groups),
                       "requests_sha256": hashlib.sha256((directory / "requests.jsonl").read_bytes()).hexdigest()})
    assert len(outcomes) == 3, "Missing configuration outcome"
    result = {"checks": checks, "all_complete": all(c["verified_complete"] for c in checks)}
    (ROOT / "results/verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["all_complete"]:
        raise SystemExit("At least one configuration did not complete; inspect preserved failure evidence.")


if __name__ == "__main__":
    main()
