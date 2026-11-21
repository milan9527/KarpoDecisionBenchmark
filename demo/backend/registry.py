"""Load the original tested datasets, preserving request strings and key order."""
import gzip
import hashlib
import json
import math


def policy_key(questions):
    return hashlib.sha256(json.dumps(questions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Registry:
    def __init__(self, root):
        config = json.loads((root / "demo/datasets.json").read_text())
        self.datasets, self.policies, self.encoded = {}, {"risk": {}, "sop": {}}, {}
        sources = {}
        for spec in config["datasets"]:
            source = spec["source"]
            if source not in sources:
                sources[source] = json.loads((root / source).read_text())
            rows = [r for r in sources[source] if not spec.get("split") or r["group"] == spec["split"]]
            entries = []
            for row in rows:
                request = row["request"]
                reversed_order = row["order"] == "reversed"
                if spec["group"] == "risk":
                    category = config["risk_titles"].get(row.get("behavior"), row["expected"]["action"])
                    title = category + " · " + row["case_id"]
                    detail = row.get("description", category)
                else:
                    state = json.loads(request["state"])
                    category = state["business_domain"].split(" (")[0]
                    boundary = next((label for key, label in config["boundary_titles"].items() if key in row["id"]), None)
                    title = category + " · " + (boundary or config["sop_titles"][row["expected"]["action"]])
                    detail = state["customer_message"]
                entries.append({"id": row["id"], "title": title + (" · 逆序" if reversed_order else " · 原序"),
                                "detail": detail, "category": category, "order": row["order"],
                                "expected": row["expected"]["action"], "request": request,
                                # JavaScript numbers otherwise erase 0.0 vs 0,
                                # changing the model's serialized state tokens.
                                "request_json": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                                "source_sha256": row["request_sha256"]})
                key = policy_key(request["questions"])
                self.policies[spec["group"]][key] = {
                    "calibrate": spec.get("calibrate", False),
                    "features": request["state"]["trusted_features"] if spec["group"] == "risk" else None}
            info = {k: spec[k] for k in ("id", "group", "title", "source")}
            info.update(count=len(entries), categories=sorted({r["category"] for r in entries}),
                        original_count=sum(r["order"] == "original" for r in entries))
            self.datasets[spec["id"]] = {**info, "questions": entries}
            self.encoded[spec["id"]] = gzip.compress(json.dumps(
                self.datasets[spec["id"]], ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)

    def metadata(self, group):
        return [{k: v for k, v in dataset.items() if k != "questions"}
                for dataset in self.datasets.values() if dataset["group"] == group]

    def validate(self, group, payload):
        if not isinstance(payload, dict) or set(payload) != {"state", "questions"}:
            raise ValueError("请求必须包含state与questions")
        policy = self.policies[group].get(policy_key(payload["questions"]))
        if not policy:
            raise ValueError("请保留所选历史问题集的完整候选与规则，仅编辑输入状态")
        if group == "risk":
            try:
                actual, expected = payload["state"]["trusted_features"], policy["features"]
                assert isinstance(actual, dict) and all(isinstance(v, dict) for v in actual.values())
                assert set(actual) == set(expected)
                assert all(set(actual[k]) == set(expected[k]) for k in expected)
                assert all(v is None or isinstance(v, (bool, int)) or isinstance(v, float) and math.isfinite(v)
                           for values in actual.values() for v in values.values())
                assert isinstance(payload["state"]["user_message"], str)
            except (KeyError, TypeError, AssertionError):
                raise ValueError("请保留所选问题集完整200个特征及其分组结构") from None
        else:
            try:
                state = json.loads(payload["state"])
                assert isinstance(state, dict) and isinstance(state["trusted_facts"], dict)
                assert isinstance(state["customer_message"], str)
            except (TypeError, KeyError, ValueError, AssertionError):
                raise ValueError("SOP state应为包含可信事实和客户消息的JSON字符串") from None
        return policy
