"""Create an isolated, reproducible promotional-abuse dataset without any GPU."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import html
import json
import math
from pathlib import Path

from promo_risk import DOMAINS, PRIORITY, REQUIRED, RULES, SCENARIOS, digest, extract, flattened, make_case

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "experiments/promo-risk-20261121"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def validate_snapshot(snapshot, features, schema):
    """Business invariants independent of the label policy."""
    now = snapshot["as_of"]
    created = snapshot["account"]["created_at"]
    for kind in ("requests", "claims", "orders", "transfers"):
        for event in snapshot[kind]:
            if not created <= event["ts"] <= now:
                raise ValueError(f"{snapshot['id']}: {kind} outside observable account lifetime")
    for order in snapshot["orders"]:
        if not 0 <= order["discount_cents"] <= order["amount_cents"]:
            raise ValueError("discount outside order amount")
        if not order["ts"] <= order["paid_at"] <= now:
            raise ValueError("invalid payment time")
        if order["refund_at"] is not None and not order["paid_at"] <= order["refund_at"] <= now:
            raise ValueError("invalid refund time")
    for claim in snapshot["claims"]:
        if claim["used"] and claim["ts"] + claim["use_delay_seconds"] > now:
            raise ValueError("future coupon use leaked")
        if claim["after_refund"] and not any(
            o["refund_at"] is not None and o["refund_at"] <= claim["ts"] for o in snapshot["orders"]
        ):
            raise ValueError("post-refund claim without preceding refund")
    for link in snapshot["links"]:
        if not link["created_at"] <= link["last_seen"] <= now or link["claim_ts"] > now:
            raise ValueError("invalid relationship time")
    for transfer in snapshot["transfers"]:
        if transfer["returned_at"] is not None and not transfer["ts"] <= transfer["returned_at"] <= now:
            raise ValueError("invalid transfer return time")
    flat = flattened(features)
    if len(flat) != 200 or len(schema) != 200 or set(flat) != {s["path"] for s in schema}:
        raise ValueError("feature schema mismatch")
    for spec in schema:
        value = flat[spec["path"]]
        if value is None:
            if spec["path"] not in snapshot["missing"]:
                raise ValueError("unexplained missing feature")
            continue
        kind = spec["type"]
        if ((kind == "boolean" and type(value) is not bool) or
                (kind == "integer" and type(value) is not int) or
                (kind == "number" and type(value) is not float)):
            raise ValueError(f"invalid feature type: {spec['path']}={value}")
        if type(value) in (int, float) and not math.isfinite(value):
            raise ValueError("non-finite feature")
        if spec["unit"] in ("ratio", "score_0_1") and not 0 <= value <= 1:
            raise ValueError(f"ratio out of range: {spec['path']}")
        if spec["unit"] not in ("signed_count", "signed_cent", "sentinel_day") and value < 0:
            raise ValueError(f"negative feature: {spec['path']}")
    counts = [features["behavior"][f"requests_{w}"] for w in ("1m", "5m", "1h", "24h")]
    if counts != sorted(counts):
        raise ValueError("non-monotone cumulative time-window counts")
    if features["orders"]["refunds_30d"] > features["orders"]["paid_orders_30d"]:
        raise ValueError("refund count exceeds payments")
    if features["resource"]["mean_job_ms"] > features["resource"]["max_job_ms"]:
        raise ValueError("average task exceeds maximum task time")


def prepare(output, tune_count=4, quality_count=12):
    if tune_count < 1 or quality_count < 1:
        raise ValueError("each partition must contain cases")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be new/empty; do not overwrite a frozen dataset")
    requests, snapshots, schema = [], [], None
    observed = defaultdict(set)
    matched_rules = Counter()
    entity_ids = {"tune": set(), "quality": set()}
    state_hashes = {"tune": set(), "quality": set()}
    for split, count in (("tune", tune_count), ("quality", quality_count)):
        for name, title, expected, category in SCENARIOS:
            for i in range(count):
                snapshot, rows, case_schema = make_case(name, title, expected, category, split, i)
                features = rows[0]["request"]["state"]["trusted_features"]
                validate_snapshot(snapshot, features, case_schema)
                if schema is None:
                    schema = case_schema
                elif schema != case_schema:
                    raise ValueError("feature schema drift")
                for path, value in flattened(features).items():
                    observed[path].add(json.dumps(value))
                for key in rows[0]["reference_rule_ids"]:
                    matched_rules[key] += 1
                entities = {snapshot["id"], snapshot["device"]["id"], snapshot["network"]["ip"],
                            snapshot["account"]["identity_id"]}
                for link in snapshot["links"]:
                    entities.update(link[key] for key in ("account", "device", "ip", "payment", "payee", "address", "identity"))
                entity_ids[split].update(entities)
                state_hashes[split].add(digest(rows[0]["request"]["state"]))
                snapshots.append(snapshot)
                requests.extend(rows)
    if entity_ids["tune"] & entity_ids["quality"] or state_hashes["tune"] & state_hashes["quality"]:
        raise ValueError("partition leakage")
    policy_features = {c["feature"] for r in RULES for c in r["all"]} | set(REQUIRED)
    if policy_features - {s["path"] for s in schema}:
        raise ValueError("policy references absent features")
    if set(matched_rules) != {r["id"] for r in RULES} | {"missing_required"}:
        raise ValueError("not every policy rule was exercised")
    for field in schema:
        field["reference_rules"] = [r["id"] for r in RULES if any(c["feature"] == field["path"] for c in r["all"])]
        if field["path"] in REQUIRED:
            field["reference_rules"].append("missing_required")
        field["role"] = "reference_policy" if field["reference_rules"] else "context"
        field["distinct_observed_values"] = len(observed[field["path"]])
    summary = {
        "status": "offline_validated_model_not_run", "dataset_kind": "scenario_driven_synthetic",
        "feature_count": 200, "domain_counts": {d: 20 for d in DOMAINS},
        "scenario_count": len(SCENARIOS), "unique_cases": len(snapshots), "requests_with_order_variants": len(requests),
        "partition_cases": {split: sum(s["split"] == split for s in snapshots) for split in ("tune", "quality")},
        "case_action_counts": dict(Counter(r["expected"]["action"] for r in requests if r["order"] == "original")),
        "case_category_counts": dict(Counter(r["category"] for r in requests if r["order"] == "original")),
        "policy_feature_count": len(policy_features), "context_feature_count": 200-len(policy_features),
        "varying_feature_count": sum(len(values) > 1 for values in observed.values()),
        "constant_features": [path for path, values in observed.items() if len(values) == 1],
        "rule_case_counts": dict(sorted(matched_rules.items())), "all_authored_labels_match_reference_policy": True,
        "entity_overlap_tune_quality": 0, "input_state_overlap_tune_quality": 0,
        "quality_and_tune_share_behavior_templates": True, "model_results": None,
        "production_accuracy": None, "latency_ms": None, "gpu_actions_performed": False,
        "serialization_bytes": {
            "min": min(len(json.dumps(r["request"], ensure_ascii=False).encode()) for r in requests),
            "max": max(len(json.dumps(r["request"], ensure_ascii=False).encode()) for r in requests),
            "note": "Bytes are not tokens. Re-encode with the pinned Karpo encoder before GPU testing.",
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "risk-feature-schema.json", {
        "version": 2, "domains": DOMAINS, "features": schema,
        "conventions": {
            "currency": "CNY cents; 100 cents = 1 CNY",
            "windows": "0 <= as_of - event_time < window_seconds unless explicitly [7d,14d)",
            "counts": "Nonnegative integers except explicitly signed remaining quotas.",
            "missing": "null; never coerced to 0 or false",
            "empty_denominator": "0.0 when the source is observed but has no eligible events",
            "labels": "Authored separately; labels, scenario names and matched rules are outside the model request.",
            "graph": "A synthetic observed neighborhood, not a production-complete entity graph.",
            "entity_ids": "Synthetic IDs exist only in source ledgers, not in the 200 model features.",
        },
    })
    write_json(output / "policy.json", {
        "status": "illustrative_business_policy_not_production_calibrated",
        "priority": PRIORITY, "rules": RULES, "required_features": REQUIRED,
        "missing_action": "review", "missing_priority": "same priority as review",
    })
    write_json(output / "scenarios.json", [
        {"id": name, "description": title, "authored_expected_action": expected, "category": category}
        for name, title, expected, category in SCENARIOS
    ])
    write_json(output / "risk-requests.json", requests)
    with (output / "source-events.jsonl").open("w") as stream:
        for snapshot in snapshots:
            stream.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_json(output / "validation.json", summary)
    (output / "examples").mkdir()
    examples = ("normal", "refund_arbitrage", "reward_pooling", "office_nat", "identity_reuse")
    for name in examples:
        row = next(r for r in requests if r["behavior"] == name and r["group"] == "quality" and r["order"] == "original")
        write_json(output / "examples" / f"{name}.request.json", row["request"])
        write_json(output / "examples" / f"{name}.expected.json", {
            "request_id": row["id"], "expected": row["expected"], "reference_rule_ids": row["reference_rule_ids"],
            "model_response": None, "note": "Business reference label, not a measured Karpo response.",
        })
    render_docs(output, schema, summary)
    manifest_files = sorted(p for p in output.rglob("*") if p.is_file())
    source_paths = [ROOT / "scripts/promo_risk.py", ROOT / "scripts/promo_risk_prepare.py",
                    ROOT / "scripts/promo_risk_evaluate.py", ROOT / "tests/test_promo_risk.py"]
    manifest = {
        "dataset": output.name, "schema_version": 2, "model_test_status": "not_run",
        "files": {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest() for p in manifest_files},
        "sources": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in source_paths if p.exists()},
    }
    write_json(output / "manifest.json", manifest)
    return summary


def render_docs(output, schema, summary):
    lines = [
        "# 薅羊毛风控：200特征与行为模拟",
        "",
        "**状态：离线数据和参考策略校验通过；尚未运行Karpo模型。没有启动EC2。**",
        "",
        "200个特征可以作为主要行为的第一版覆盖范围。覆盖字段不等于验证效果；"
        "本轮是按业务场景构造的合成事件流，不是客户真实数据或真实作弊分布。",
        "",
        f"共{summary['scenario_count']}种行为、{summary['unique_cases']}个实体隔离案例，"
        f"每例原序/倒序两次输入，共{summary['requests_with_order_variants']}条请求。"
        f"tune={summary['partition_cases']['tune']}例，quality={summary['partition_cases']['quality']}例。"
        "两种顺序属于同一案例，不能按两份独立样本计算置信区间。",
        "",
        f"参考策略明确使用{summary['policy_feature_count']}个特征，其余"
        f"{summary['context_feature_count']}个是有定义、有来源的上下文特征。"
        "没有宣称全部200项都已证明有效，也没有用随机数填满字段。"
        f"当前场景中有{summary['varying_feature_count']}个字段发生变化；恒定字段在validation.json中列出。",
        "",
        "[可检索HTML](report.html) · [200特征字典](risk-feature-schema.json) · "
        "[完整模型请求](risk-requests.json) · [原始合成事件](source-events.jsonl) · "
        "[业务参考策略](policy.json) · [离线校验](validation.json)",
        "",
        "## 数据和标签",
        "",
        "- 按账户生命周期生成请求、领券、订单、资金流和关联账号快照，再聚合窗口计数、比例、变化和关系特征。",
        "- 每个字段有类型、单位、窗口、来源、缺失约定及是否参与参考策略的说明。金额使用人民币分。",
        "- 正常样例包含新客、家庭共用设备、公司共用IP、正常退款、活动高峰、正常重试、非奖励共享收款、"
        "真实推荐购买、允许叠加优惠、配额内批量任务、历史有事件但当前正常以及多设备支付。",
        "- 异常样例包含批量注册、自动领券、重复权益、违规叠加、退款套利、推荐奖励、收益归集、同步团伙、资金回流、代理切换及资源耗尽。",
        "- 阈值下方/等于阈值、规则冲突、关键/非关键缺失、用户文本注入分别覆盖。",
        "- 预期动作在场景表独立编写，参考规则只核对标签。模型输入不含预期动作、行为名称、类别或命中规则。",
        "- quality与tune使用不同种子和实体，输入无重叠，但共享行为模板；这不是对未知作弊分布的泛化证明。",
        "- 仅使用决策时刻已知的数据；不使用当前请求未来退款或事后调查结果。图关系是模拟的已观测关联邻域。",
        "",
        "## 后续模型验收",
        "",
        "1. 固定当前字典、参考策略和quality集。策略阈值是业务示例，需要客户确认；不能根据quality结果改标签。",
        "2. 用固定版本Karpo Encoder重新计算全部请求token、marker和哈希，确认不超8192 token。JSON字节数不能替代token数。",
        "3. 新模型测试需建立新的运行目录和编码基线，不能使用旧200字段的encoding-reference或旧套件硬编码路径。",
        "4. FP8/vLLM完整200字段输入，先质量、再固定100 RPS两轮各120秒。报告所有错误、端到端P50/P95和排队时间。",
        "5. 质量按动作、行为、正常用户、边界、缺失和换序分别统计；报告作弊放行率和正常用户误干预率，不只看总准确率。",
        "6. 目标仍为正确率≥90%、100 RPS、端到端P50≤500ms。漏放/误伤目标需按业务损失单独确定。",
        "7. 评分工具要求新压测器提供request_sha256、HTTP状态、完整响应、latency_ms及valid=true；"
        "valid必须包括新版编码一致性校验。缺失请求和编码校验失败均计为失败。",
        "",
        "旧四RTX的97.08%和约65ms对应旧输入与策略；本数据更复杂，性能和质量均需重测。"
        "所有新模型结果保持null，不用参考规则一致率冒充模型准确率。",
        "",
        "重新生成到新的空目录：",
        "",
        "```bash",
        "python scripts/promo_risk_prepare.py --output /tmp/promo-risk-reproduction",
        "python -m unittest discover -s tests -p 'test_promo_risk.py'",
        "# 取得新模型逐条响应之后再评价：",
        "python scripts/promo_risk_evaluate.py --fixtures experiments/promo-risk-20261121/risk-requests.json "
        "--responses /path/to/new/quality/requests.jsonl --output /tmp/promo-quality.json",
        "```",
        "",
        "## 场景覆盖",
        "",
        "| 场景 | 类别 | 预期动作 |",
        "|---|---|---|",
    ]
    lines += [f"| {title} | {category} | {expected} |" for _,title,expected,category in SCENARIOS]
    lines += ["", "## 完整示例", "",
              "[退款套利输入](examples/refund_arbitrage.request.json) · "
              "[参考答案（非模型输出）](examples/refund_arbitrage.expected.json) · "
              "[公司共享IP正常输入](examples/office_nat.request.json)", "",
              "## 200特征字典", "",
              "| 字段 | 含义 | 类型/单位 | 窗口 | 参考策略 |", "|---|---|---|---|---|"]
    for f in schema:
        lines.append(f"| `{f['path']}` | {f['description_zh']} | {f['type']}/{f['unit']} | {f['window']} | "
                     f"{', '.join(f['reference_rules']) or '上下文'} |")
    (output / "README.zh.md").write_text("\n".join(lines) + "\n")
    h = html.escape
    rows = "".join(
        f"<tr><td>{h(f['path'])}</td><td>{h(f['description_zh'])}</td>"
        f"<td>{h(f['type'])} / {h(f['unit'])}</td><td>{h(f['window'])}</td>"
        f"<td>{h(', '.join(f['reference_rules']) or '上下文')}</td><td>{h(f['source'])}</td></tr>"
        for f in schema
    )
    scenarios = "".join(
        f"<tr><td>{h(title)}</td><td>{h(category)}</td><td>{h(expected)}</td></tr>"
        for _,title,expected,category in SCENARIOS
    )
    (output / "report.html").write_text(f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>薅羊毛风控 · 200特征</title>
<style>
body{{font:16px/1.65 system-ui,sans-serif;background:#f5f7fb;color:#14243b;max-width:1400px;margin:auto;padding:32px}}
h1,h2{{line-height:1.3}} a{{color:#1455a3}} .status{{background:#fff4d4;border-left:5px solid #bb7900;padding:16px}}
section{{background:white;padding:24px;margin:24px 0;border-radius:12px}} .scroll{{overflow:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #dae1ed}}
th{{background:#edf3fc}}input{{box-sizing:border-box;width:100%;padding:12px;font:inherit;margin:12px 0}}
pre{{overflow:auto;max-height:600px;background:#f1f5fa;padding:16px}}[hidden]{{display:none!important}}
@media print{{input{{display:none}}body{{padding:0}}section{{break-inside:auto}}}}
</style>
<h1>薅羊毛风控：200个特征</h1>
<p class="status"><strong>离线校验完成 · 模型尚未测试</strong><br>
这是场景驱动的合成数据。新准确率、漏放率、误伤率和延迟尚无实测结果。EC2未启动。</p>
<section><h2>覆盖范围</h2>
<p>10类 × 20个特征；{summary['scenario_count']}种行为；{summary['unique_cases']}个案例；
含换序共{summary['requests_with_order_variants']}条请求。
参考策略使用{summary['policy_feature_count']}个特征，{summary['context_feature_count']}个作为上下文。</p>
<p>包含正常新客、家庭共享设备、公司共享IP等误伤对照。quality和tune隔离实体及种子，但共享行为模板。
原四RTX测试的准确率与延迟不能沿用到本数据。</p>
<p><a href="README.zh.md">说明与验收计划</a> · <a href="risk-feature-schema.json">下载特征字典</a> ·
<a href="risk-requests.json">下载完整请求</a> · <a href="validation.json">离线校验结果</a></p></section>
<section><h2>行为场景及参考动作</h2><div class="scroll"><table><thead><tr><th>场景</th><th>类别</th><th>业务预期</th></tr></thead>
<tbody>{scenarios}</tbody></table></div></section>
<section><h2>完整输入示例</h2><p><a href="examples/refund_arbitrage.request.json">退款套利：完整200字段输入</a> ·
<a href="examples/refund_arbitrage.expected.json">参考答案（非模型响应）</a> ·
<a href="examples/office_nat.request.json">公司共享IP：正常输入</a></p></section>
<section><h2>可检索特征字典</h2><label for="search">搜索字段、中文含义、来源或规则</label>
<input id="search" type="search" placeholder="例如：退款、promotion、graph、上下文">
<p id="count">显示200个特征</p><div class="scroll"><table><thead><tr><th>字段</th><th>含义</th>
<th>类型 / 单位</th><th>窗口</th><th>参考规则</th><th>来源</th></tr></thead><tbody id="features">{rows}</tbody></table></div></section>
<script>
document.getElementById('search').addEventListener('input',e=>{{
const q=e.target.value.toLowerCase().trim();let n=0;
document.querySelectorAll('#features tr').forEach(r=>{{r.hidden=!r.textContent.toLowerCase().includes(q);if(!r.hidden)n++;}});
document.getElementById('count').textContent='显示'+n+'个特征';
}});
</script></html>
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--tune-per-scenario", type=int, default=4)
    parser.add_argument("--quality-per-scenario", type=int, default=12)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.tune_per_scenario, args.quality_per_scenario), ensure_ascii=False, indent=2))
