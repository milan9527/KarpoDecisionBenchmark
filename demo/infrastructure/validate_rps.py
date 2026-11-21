"""Exercise the deployed fixed-RPS browser, original datasets, and real GPUs.

Each accepted workload runs twice at 100 RPS for 120 seconds. Reports include
every attempt; full exports are gzip compressed. Only this test's leases end.
"""
import asyncio
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import time

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-rps-20261121"
FRONTEND = ("index.html", "app.js", "benchmark.js", "rate-client.js", "rate-worker.js", "history.js", "history-worker.js", "cloud-client.js", "style.css")
TOKEN_SOURCES = {
    "promo-tune": "experiments/promo-rtx6-20261121/results/runs/rtx6-calibrated-v1/confirm/requests.jsonl",
    "promo-quality": "experiments/promo-rtx6-20261121/results/runs/rtx6-calibrated-v1/quality/requests.jsonl",
    "sop-full": "experiments/vllm-sop-h200-20261120/results/runs/h200-fp8-v2/confirmation/b4/r8/sop/quality/requests.jsonl",
}
TOKEN_REFERENCE = {}


def check_metrics(report):
    rows, m = report["records"], report["metrics"]
    good = [r for r in rows if r["ok"]]
    assert m["completed"] == len(rows) == m["started"]
    assert m["successful"] == len(good) and m["failed"] == len(rows) - len(good)
    assert m["active"] == 0
    assert len({r["index"] for r in rows}) == len(rows)
    assert math.isclose(m["successful_rps"], len(good) * 1000 / m["duration_ms"])
    for name, p in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99)):
        values = sorted(r["elapsed_ms"] for r in good)
        assert m[name] == (values[math.ceil(len(values) * p) - 1] if values else None)
    end, start = m["offered_seconds"], m["offered_seconds"] * .25
    steady = [r for r in good if start <= r["end_offset_s"] < end]
    if not m["stopped"]:
        assert math.isclose(m["steady_valid_rps"], len(steady) / (end - start))
    correct = 0
    for r in rows:
        assert math.isclose(r["scheduled_offset_s"], (r["index"] - 1) / m["target_rps"])
        assert math.isclose(r["elapsed_ms"], (r["end_offset_s"] - r["scheduled_offset_s"]) * 1000, abs_tol=1e-6)
        if r["ok"]:
            response = r["response"]
            assert response["demo"]["live_model"] is True
            choice = response.get("decision", {}).get("action") or response["answers"]["action"]["choice"]
            q = report["questions"][r["question_index"]]
            if "expected" in q:
                correct += choice == q["expected"]
    assert correct == m["correct"]
    if m["evaluated"]:
        assert math.isclose(m["accuracy"], correct / m["evaluated"])
    dataset = report["dataset_id"]
    if dataset in TOKEN_SOURCES:
        if dataset not in TOKEN_REFERENCE:
            with (ROOT / TOKEN_SOURCES[dataset]).open() as stream:
                TOKEN_REFERENCE[dataset] = {r["request_id"]: r["response"]["metrics"]["tokens_sha256"]
                    for r in map(json.loads, stream) if r["valid"]}
        reference = TOKEN_REFERENCE[dataset]
        for row in good:
            q = report["questions"][row["question_index"]]
            if "expected" in q:
                assert q["id"] in reference, q["id"]
                assert row["response"]["metrics"]["tokens_sha256"] == reference[q["id"]], q["id"]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", nargs="+", choices=("risk", "sop"), default=["risk", "sop"])
    parser.add_argument("--resume", action="store_true", help="Append to existing evidence; retain previous runs")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    resources = json.loads((ROOT / "operations/demo-20261121/resources.json").read_text())
    base = "https://" + resources["domain"]
    evidence = {"url": base, "started_at": datetime.now(timezone.utc).isoformat(),
                "scope": "Public CloudFront browser fixed-arrival 100 RPS, original requests, real GPUs",
                "client": {"region": "us-east-1", "instance_type": "m8g.2xlarge", "browser": "Chromium / dedicated Web Worker"},
                "runs": [], "checks": {}, "frontend_sha256": {
                    name: hashlib.sha256((ROOT / "demo/frontend" / name).read_bytes()).hexdigest()
                    for name in FRONTEND}}
    if args.resume and (OUT / "validation.json").exists():
        evidence = json.loads((OUT / "validation.json").read_text())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        session_file = ROOT / ".state/demo-rps-session.json"
        context = await browser.new_context(viewport={"width": 1600, "height": 1100},
            **({"storage_state": str(session_file)} if session_file.exists() else {}))
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        acquired = set()
        try:
            if (await context.request.get(base + "/api/catalog")).status == 401:
                credentials = json.loads((ROOT / ".state/demo-login.json").read_text())
                assert (await context.request.post(base + "/api/auth", data=credentials)).status == 200
            for filename in FRONTEND:
                response = await context.request.get(base + "/" + filename)
                assert await response.body() == (ROOT / "demo/frontend" / filename).read_bytes(), filename
            anonymous = await playwright.request.new_context()
            assert (await anonymous.get(base + "/api/datasets/promo-tune")).status == 401
            await anonymous.dispose()
            catalog = await (await context.request.get(base + "/api/catalog")).json()
            datasets = {}
            for group in ("risk", "sop"):
                for spec in catalog[group]["datasets"]:
                    data = await (await context.request.get(base + "/api/datasets/" + spec["id"])).json()
                    assert len(data["questions"]) == spec["count"]
                    sources = {r["id"]: r for r in json.loads((ROOT / spec["source"]).read_text())}
                    for q in data["questions"]:
                        assert json.dumps(q["request"], ensure_ascii=False) == json.dumps(
                            sources[q["id"]]["request"], ensure_ascii=False)
                        assert q["source_sha256"] == sources[q["id"]]["request_sha256"]
                        assert json.dumps(json.loads(q["request_json"])) == json.dumps(sources[q["id"]]["request"])
                        assert "expected" not in q["request"]
                    datasets[spec["id"]] = data
            evidence["checks"]["original_datasets"] = {k: len(v["questions"]) for k, v in datasets.items()}
            print("All 1904 original inputs verified.", flush=True)
            # A prior watchdog stop must finish before a new start can be accepted.
            deadline = time.monotonic() + 1800
            while len(acquired) < len(set(args.groups)):
                for group in set(args.groups) - acquired:
                    response = await context.request.post(base + f"/api/{group}/start", data={})
                    if response.status == 200:
                        acquired.add(group)
                    else:
                        assert response.status == 409, await response.text()
                assert time.monotonic() < deadline, "GPU stop/start timed out"
                if len(acquired) < len(set(args.groups)):
                    await asyncio.sleep(10)
            await page.goto(base, wait_until="networkidle")
            await expect(page.locator("#bench-rate")).to_have_value("100")
            await expect(page.locator("#bench-seconds")).to_have_value("120")
            for group in args.groups:
                await page.locator(f".tab[data-group={group}]").click()
                await page.locator("#bench-execution").select_option("browser")
                while await page.locator("#bench-run").is_disabled():
                    assert time.monotonic() < deadline, "Readiness timed out: " + group
                    await asyncio.sleep(5)
                    state = await (await context.request.get(base + "/api/status")).json()
                    for active in acquired:
                        if state["groups"][active]["session_active"]:
                            await context.request.post(base + f"/api/{active}/activity", data={})
                    assert not state["groups"][group].get("error"), state["groups"][group]["error"]
                print(group, "ready", flush=True)

                async def select(dataset):
                    await page.locator("#bench-dataset").select_option(dataset)
                    await expect(page.locator("#bench-selection-count")).to_contain_text(
                        f'{len(datasets[dataset]["questions"])}', timeout=60000)
                    await expect(page.locator("#bench-dataset")).to_be_enabled(timeout=60000)
                    await expect(page.locator("#bench-run")).to_be_enabled()

                async def run(name, seconds, rate=100, stop=False):
                    original_name, attempt = name, 1
                    while (OUT / (name + ".json.gz")).exists():
                        attempt += 1
                        name = original_name + f"-attempt-{attempt}"
                    await page.locator("#bench-rate").fill(str(rate))
                    await page.locator("#bench-seconds").fill(str(seconds))
                    await page.locator("#bench-run").click()
                    if stop:
                        await expect(page.locator("#bench-stop")).to_be_enabled()
                        await asyncio.sleep(.3)
                        await page.locator("#bench-stop").click()
                    await expect(page.locator("#bench-export")).to_be_enabled(timeout=240000)
                    async with page.expect_download() as event:
                        await page.locator("#bench-export").click()
                    download = await event.value
                    temp = ROOT / ".state" / (name + ".json")
                    await download.save_as(str(temp))
                    report = json.loads(temp.read_text())
                    check_metrics(report)
                    raw = temp.read_bytes()
                    path = OUT / (name + ".json.gz")
                    path.write_bytes(gzip.compress(raw, compresslevel=6, mtime=0))
                    temp.unlink()
                    steady = report["metrics"]["steady_valid_rps"]
                    assert (await page.locator("#bench-rps").inner_text()) == (
                        f"{steady:.1f}" if steady is not None else "—")
                    if report["metrics"]["p50_ms"] is not None:
                        assert (await page.locator("#bench-p50").inner_text()) == f'{report["metrics"]["p50_ms"]:.1f}'
                    evidence["runs"].append({"name": name, "file": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "dataset_id": report["dataset_id"], "metrics": report["metrics"]})
                    (OUT / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
                    m = report["metrics"]
                    print(name, json.dumps({k: m[k] for k in (
                        "completed", "successful", "failed", "steady_valid_rps", "p50_ms", "p95_ms",
                        "max_send_lag_ms", "accuracy", "passes_performance_acceptance")}), flush=True)
                    # Cloud history sync follows measurement and must finish
                    # before a subsequent run. Its time is outside the report.
                    await expect(page.locator("#history-sync")).to_contain_text("完整测试记录已保存", timeout=300000)
                    await expect(page.locator("#bench-rate")).to_be_enabled(timeout=60000)
                    return report

                primary = "promo-tune" if group == "risk" else "sop-full"
                await select(primary)
                # Filters and selection are independent: "only filtered" sets the run subset.
                await page.locator("#bench-order").select_option("original")
                await page.locator("#bench-select-filter").click()
                await expect(page.locator("#bench-selection-count")).to_contain_text(
                    f'已选 {datasets[primary]["original_count"]}')
                await page.locator("#bench-order").select_option("")
                await page.locator("#bench-select-all").click()
                warm = await run(group + "-warmup", 20, rate=20)
                assert warm["metrics"]["failed"] == 0, warm["metrics"]
                for round_ in (1, 2):
                    # Renew the other workload while this one's inference renews its own.
                    for active in acquired - {group}:
                        await context.request.post(base + f"/api/{active}/activity", data={})
                    report = await run(group + f"-100rps-120s-r{round_}", 120)
                    assert len(report["questions"]) == len(datasets[primary]["questions"])
                    assert [q["request"] for q in report["questions"]] == [
                        q["request"] for q in datasets[primary]["questions"]]
                await page.locator("#bench-results .question-edit").first.click()
                detail = json.loads(await page.locator("#bench-detail-json").text_content())
                assert "request" in detail and "response" in detail
                await page.locator("#bench-detail summary").click()
                await page.locator('[aria-labelledby="benchmark-title"]').screenshot(path=str(OUT / (group + "-rps.png")))
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await page.locator('[aria-labelledby="benchmark-title"]').screenshot(path=str(OUT / (group + "-mobile.png")))
                await page.set_viewport_size({"width": 1600, "height": 1100})
                if group == "risk":
                    for dataset, seconds in (("promo-quality", 11), ("resource-risk", 4)):
                        await select(dataset)
                        report = await run(dataset + "-coverage", seconds)
                        assert report["metrics"]["failed"] == 0, report["metrics"]
                        assert len({r["question_index"] for r in report["records"]}) == len(datasets[dataset]["questions"])
                        if dataset == "resource-risk":
                            assert all("decision" not in r["response"] for r in report["records"])
                    stopped = await run("risk-stopped", 120, stop=True)
                    assert 0 < stopped["metrics"]["started"] < 12000
                    assert stopped["metrics"]["unsent"] > 0
            if group == "sop":
                # Invalid edited state is counted as a failure and is never retried.
                await page.locator("#state").fill('{"customer_message":"missing trusted facts"}')
                await page.locator("#bench-add").click()
                await page.locator("#bench-clear").click()
                await page.locator("#bench-questions input").last.check()
                failed = await run("sop-invalid-input", 1, rate=2)
                assert failed["metrics"]["failed"] == 2 and failed["metrics"]["successful"] == 0
                assert all(r["status"] == 422 for r in failed["records"])
            assert not errors, errors
            evidence["checks"].update(deployed_frontend_matches=True, defaults_100rps_120s=True,
                mobile_no_overflow=True, complete_original_inputs_preserved=True,
                model_token_hashes_match_original_benchmarks=True,
                filter_and_select=True, expected_labels_outside_model_request=True,
                stop_drains_inflight=any(r["name"].startswith("risk-stopped") for r in evidence["runs"]),
                invalid_requests_counted=any(r["name"].startswith("sop-invalid-input") for r in evidence["runs"]),
                exported_metrics_independently_recomputed=True)
        finally:
            for group in acquired:
                await context.request.post(base + f"/api/{group}/end", data={})
            evidence["after_end"] = await (await context.request.get(base + "/api/status")).json()
            evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
            evidence["page_errors"] = errors
            (OUT / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            await context.close()
            await browser.close()
    session_file.unlink(missing_ok=True)
    print("Fixed-RPS browser validation complete; this test's GPU leases ended.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
