"""Import every committed benchmark summary and browser export, without rerunning GPUs.

One entry per stage/batch. Preserve original metrics and every available response.
Input joins require an exact request hash; missing evidence stays explicitly missing.
Re-imports use deterministic IDs and immutable content hashes.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "demo/backend"))
from history import packed

COMMIT = "6a05175"
REPO = "https://github.com/milan9527/KarpoDecisionBenchmark/blob/" + COMMIT + "/"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def normalized(m):
    completed = m.get("completed", m.get("attempts", m.get("requests")))
    success = m.get("successful", m.get("valid"))
    accuracy = m.get("accuracy", m.get("accuracy_all_attempts"))
    if accuracy is None and completed and "correct" in m:
        accuracy = m["correct"] / completed
    return {"completed": completed, "successful": success, "failed": m.get("failed", m.get("failures")),
            "correct": m.get("correct"), "accuracy": accuracy, "p50_ms": m.get("p50_ms"),
            "p95_ms": m.get("p95_ms"), "p99_ms": m.get("p99_ms"),
            "steady_valid_rps": m.get("steady_valid_rps"),
            "successful_rps": m.get("successful_rps", m.get("valid_rps_with_drain", m.get("valid_rps"))),
            "target_rps": m.get("target_rps"), "offered_seconds": m.get("offered_seconds"),
            "passes_performance_acceptance": m.get("passes_performance_acceptance")}


def stages(value):
    if isinstance(value, list):
        return [(f"batch-{r.get('batch', i)}", r) for i, r in enumerate(value)]
    if "attempts" in value:
        return [("", value)]
    return [(name, r) for name, r in value.items() if isinstance(r, dict) and "attempts" in r]


def input_index(files):
    index = {}
    for path in files:
        if path.suffix != ".json" or not (path.name.endswith("requests.json") or path.as_posix() == "fixtures/payment.json"):
            continue
        rows = json.loads((ROOT / path).read_text())
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or "request" not in row:
                continue
            request = row["request"]
            # Historical clients used different canonicalization conventions.
            hashes = {row.get("request_sha256")}
            for compact in (True, False):
                for ascii_ in (True, False):
                    for sort in (True, False):
                        raw = json.dumps(request, ensure_ascii=ascii_, sort_keys=sort,
                                         **({"separators": (",", ":")} if compact else {})).encode()
                        hashes.add(digest(raw))
            for hash_ in hashes - {None}:
                index.setdefault(hash_, (row, path.as_posix()))
    return index


def question(row, inputs):
    source = inputs.get(row.get("request_sha256"))
    result = {"id": row.get("request_id", ""), "title": row.get("request_id", "历史问题"),
              "source_sha256": row.get("request_sha256"), "expected": row.get("expected", {}).get("action")}
    if source:
        original, path = source
        result.update(request=original["request"], request_json=packed(original["request"]), input_source=path)
    else:
        result["missing_input"] = "仓库中未找到与该请求哈希匹配的原始输入；保留实际响应及请求哈希。"
    return result


def date_for(path, rows, report=None):
    if report and report.get("started_at"):
        return report["started_at"], "timestamp"
    times = [r["started_utc"] for r in rows if r.get("started_utc")]
    if times:
        return min(times), "timestamp"
    match = re.search(r"20[0-9]{6}", str(path))
    if match:
        return datetime.strptime(match[0], "%Y%m%d").date().isoformat(), "experiment-date-only"
    return None, "not-recorded"


def make_doc(path, stage, metrics, rows, report=None):
    date, precision = date_for(path, rows, report)
    epoch = int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000) if date else 0
    source = path.as_posix()
    id_ = f"{epoch:013d}_import_{digest((source+'#'+stage).encode())[:24]}"
    scenario = (report or {}).get("scenario")
    if not scenario:
        scenario = "risk" if "/risk/" in source or "promo-" in source else "sop"
    kind = "fixed-rps" if "target_rps" in metrics else "quality" if "quality" in source+stage else "closed" if (
        metrics.get("mode") == "closed" or "concurrency" in stage or "configured_concurrency" in metrics) else "historical"
    doc = {"id": id_, "title": source.replace("/summary.json", "").replace("experiments/", "")+(" · "+stage if stage else ""),
           "scenario": scenario, "kind": kind, "dataset_id": (report or {}).get("dataset_id", ""),
           "started_at": date, "date_precision": precision, "updated_epoch": 0,
           "origin": "import", "status": "stopped" if metrics.get("stopped") else "archived",
           "source": source, "source_sha256": digest((ROOT/path).read_bytes()),
           "source_stage": stage, "source_commit": COMMIT,
           "sources": [{"label": "原始报告", "url": REPO+source}],
           "metrics": normalized(metrics), "raw_metrics": metrics,
           "measurement": metrics.get("latency_scope") or (report or {}).get("timing") or "以原始报告口径为准",
           "record_count": len(rows), "storage": "chunks",
           "evidence_note": "导入原始记录；缺失字段不补造，不按当前硬件重新解释旧结果。"}
    if "float-diagnostic" in source:
        doc["metrics"]["passes_performance_acceptance"] = None
        doc["evidence_note"] = "输入浮点序列化改变了模型 tokens，仅用于诊断，不是原始输入验收；原始数值判定仍保留在 raw_metrics。"
    if rows and metrics.get("attempts", metrics.get("completed")) != len(rows):
        doc["evidence_note"] += " 摘要数量与可用逐条记录数量不同，请查看原始证据。"
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = [Path(p) for p in subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()]
    summaries = [p for p in files if p.name == "summary.json" and p.parts[0] in ("experiments", "results")]
    browsers = []
    for path in files:
        if path.parts[0] == "operations" and path.name == "validation.json":
            value = json.loads((ROOT/path).read_text())
            for run in value.get("runs", []):
                if "file" in run and (ROOT/path.parent/run["file"]).exists():
                    browsers.append(path.parent/run["file"])
    # Include exports saved before a validator itself aborted.
    browsers += [p for p in files if p.parts[0] == "operations" and p.name.endswith(".json.gz")]
    inputs = input_index(files)
    state = json.loads((ROOT/"operations/demo-20261121/resources.json").read_text())
    sdk = Config(max_pool_connections=16, retries={"max_attempts": 5})
    s3 = boto3.client("s3", region_name=state["region"], config=sdk)
    table = boto3.resource("dynamodb", region_name=state["region"], config=sdk).Table(state["history_table"])
    inventory, missing = [], Counter()
    existing = {}
    if args.apply:
        from boto3.dynamodb.conditions import Key
        options = {"KeyConditionExpression": Key("pk").eq("runs")}
        while True:
            page = table.query(**options)
            for item in page["Items"]:
                doc = json.loads(item["document"])
                if doc.get("origin") == "import":
                    existing[doc["id"]] = doc
            if "LastEvaluatedKey" not in page:
                break
            options["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    pool = ThreadPoolExecutor(max_workers=12)

    def upload(doc, questions, records):
        doc["question_count"] = len(questions)
        missing[doc["scenario"]] += sum("missing_input" in q for q in questions)
        inventory.append({k: doc.get(k) for k in ("id", "source", "source_stage", "source_sha256", "scenario", "record_count", "question_count")})
        if not args.apply or doc["id"] in existing:
            return
        tasks = []
        for kind, rows, size in (("q", questions, 20), ("r", records, 50)):
            for n in range(0, len(rows), size):
                part = f"{kind}{n//size:06d}"
                raw = packed(rows[n:n+size]).encode()
                hash_ = digest(raw)
                key = f"history/{doc['id']}/{part}-{hash_}.json.gz"
                marker = {"part": part, "count": len(rows[n:n+size]), "sha256": hash_, "key": key}
                tasks.append((key, gzip.compress(raw, mtime=0), {"pk": doc["id"], "sk": part, "document": packed(marker)}))
        def put(task):
            key, raw, item = task
            s3.put_object(Bucket=state["bucket"], Key=key, Body=raw, ContentType="application/json",
                          ContentEncoding="gzip", ServerSideEncryption="AES256")
            table.put_item(Item=item)
        list(pool.map(put, tasks))
        table.put_item(Item={"pk": "runs", "sk": doc["id"], "document": packed(doc)},
                       ConditionExpression="attribute_not_exists(pk)")

    for n, path in enumerate(summaries):
        value = json.loads((ROOT/path).read_text())
        rowfile = path.parent/"requests.jsonl"
        grouped = defaultdict(list)
        if (ROOT/rowfile).exists():
            with (ROOT/rowfile).open() as stream:
                for row in map(json.loads, stream):
                    grouped[row.get("stage", "")].append(row)
        for stage, metrics in stages(value):
            rows = grouped.get(stage, [])
            doc = make_doc(path, stage, metrics, rows)
            if (ROOT/rowfile).exists():
                doc["sources"].append({"label": "原始逐条响应", "url": REPO+rowfile.as_posix()})
            questions, records, seen = [], [], {}
            for row in rows:
                key = (row.get("request_id"), row.get("request_sha256"))
                if key not in seen:
                    seen[key] = len(questions); questions.append(question(row, inputs))
                records.append({**row, "index": len(records)+1, "question_index": seen[key],
                                "title": row.get("request_id", ""), "ok": row.get("valid", False),
                                "elapsed_ms": row.get("latency_ms"), "status": row.get("http_status")})
            upload(doc, questions, records)
        if n % 25 == 0:
            print(f"Historical summaries {n+1}/{len(summaries)}; entries {len(inventory)}", flush=True)
    for path in sorted(set(browsers)):
        raw = (ROOT/path).read_bytes()
        report = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        if not all(k in report for k in ("records", "metrics", "questions")):
            continue
        doc = make_doc(path, "", report["metrics"], report["records"], report)
        doc["settings"] = {k: report[k] for k in ("schema", "load", "concurrency") if k in report}
        upload(doc, report["questions"], report["records"])
        print("Browser export:", path.name, flush=True)
    pool.shutdown()
    result = {"source_commit": COMMIT, "summary_files": len(summaries),
              "browser_export_files": len(set(browsers)), "entries": len(inventory),
              "by_scenario": dict(Counter(r["scenario"] for r in inventory)),
              "available_response_records": sum(r["record_count"] for r in inventory),
              "unmatched_input_questions": dict(missing),
              "applied": args.apply, "records": inventory}
    out = ROOT/"operations/demo-history-20261121"
    out.mkdir(exist_ok=True)
    (out/("import.json" if args.apply else "inventory.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(packed({k: v for k, v in result.items() if k != "records"}))


if __name__ == "__main__":
    main()
