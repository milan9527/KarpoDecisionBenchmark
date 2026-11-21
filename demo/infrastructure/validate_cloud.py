"""Validate real AWS load generation through CloudFront, including browser outages."""
import asyncio
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time

import boto3
from playwright.async_api import async_playwright, expect

from validate_rps import check_metrics, FRONTEND

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-cloud-20261121"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    resources = json.loads((ROOT / "operations/demo-20261121/resources.json").read_text())
    base = "https://" + resources["domain"]
    table = boto3.resource("dynamodb", region_name=resources["region"]).Table(resources["history_table"])
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "url": base,
                "scope": "AWS us-east-2 → public CloudFront → ALB → API → six RTX GPUs → complete response",
                "local_browser_network_in_latency": False, "runs": [], "checks": {}}
    if (OUT / "validation.json").exists():
        evidence = json.loads((OUT / "validation.json").read_text())

    def save():
        (OUT / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")

    def read_job(run):
        return json.loads(table.get_item(Key={"pk": "cloud-jobs", "sk": run},
                                        ConsistentRead=True)["Item"]["document"])

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        session = ROOT / ".state/demo-cloud-session.json"
        context = await browser.new_context(viewport={"width": 1600, "height": 1100},
            **({"storage_state": str(session)} if session.exists() else {}))
        page = await context.new_page()
        browser_infer, errors = [], []
        context.on("request", lambda r: browser_infer.append(r.url)
                   if r.method == "POST" and r.url.endswith("/infer") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        acquired = False
        try:
            if (await context.request.get(base + "/api/catalog")).status == 401:
                credentials = json.loads((ROOT / ".state/demo-login.json").read_text())
                response = await context.request.post(base + "/api/auth", data=credentials)
                assert response.status == 200
            await context.storage_state(path=str(session))
            deadline = time.monotonic() + 1800
            while not acquired:
                response = await context.request.post(base + "/api/risk/start", data={})
                if response.status == 200:
                    acquired = True
                else:
                    assert response.status == 409, await response.text()
                    await asyncio.sleep(10)
                assert time.monotonic() < deadline, "GPU startup timed out"
            print("Risk lease acquired; waiting for six GPUs.", flush=True)
            for name in FRONTEND:
                response = await context.request.get(base + "/" + name)
                assert await response.body() == (ROOT / "demo/frontend" / name).read_bytes(), name
            await page.goto(base, wait_until="networkidle")
            await expect(page.locator("#bench-execution")).to_have_text("AWS 云端执行")
            assert await page.locator("select#bench-execution").count() == 0
            while await page.locator("#bench-run").is_disabled():
                status = await (await context.request.get(base + "/api/status")).json()
                state = status["groups"]["risk"]
                print("readiness", state["phase"], state.get("ready_gpus"), flush=True)
                assert not state.get("error"), state
                assert time.monotonic() < deadline, "GPU readiness timed out"
                if state["session_active"]:
                    await context.request.post(base + "/api/risk/activity", data={})
                await asyncio.sleep(20)
            worker = await (await context.request.get(base + "/api/cloud/active")).json()
            assert worker["worker_ready"] and not worker["job"], worker
            await page.locator("#bench-dataset").select_option("promo-tune")
            await expect(page.locator("#bench-selection-count")).to_contain_text("360")
            await expect(page.locator("#bench-run")).to_be_enabled(timeout=60000)
            cdp = await context.new_cdp_session(page)

            async def run(name, rate, seconds, mode=None):
                suffix, original = 1, name
                while (OUT / (name + ".json.gz")).exists():
                    suffix += 1
                    name = original + f"-attempt-{suffix}"
                await page.locator("#bench-rate").fill(str(rate))
                await page.locator("#bench-seconds").fill(str(seconds))
                if mode == "slow":
                    await cdp.send("Network.enable")
                    await cdp.send("Network.emulateNetworkConditions", {
                        "offline": False, "latency": 300, "downloadThroughput": 1048576,
                        "uploadThroughput": 16384})
                await page.locator("#bench-run").click()
                await expect(page.locator("#bench-stop")).to_be_enabled(timeout=60000)
                run_id = await page.evaluate("() => benchmarkRunner.id")
                print(name, run_id, "started", flush=True)
                if mode == "offline":
                    await asyncio.sleep(15)
                    before = await asyncio.to_thread(read_job, run_id)
                    await context.set_offline(True)
                    await asyncio.sleep(15)
                    after = await asyncio.to_thread(read_job, run_id)
                    assert after["metrics"]["completed"] - before["metrics"]["completed"] >= 1200
                    await context.set_offline(False)
                    await page.reload(wait_until="networkidle")
                    await expect(page.locator("#bench-stop")).to_be_enabled(timeout=60000)
                    assert await page.evaluate("() => benchmarkRunner.id") == run_id
                    evidence["checks"]["offline_and_reload"] = {
                        "run": run_id, "offline_seconds": 15,
                        "completed_during_offline": after["metrics"]["completed"] - before["metrics"]["completed"],
                        "same_job_restored": True}
                if mode == "stop":
                    # Let the independent worker claim the job before testing
                    # drain behavior; stopping while queued is tested separately.
                    await asyncio.sleep(3)
                    await page.locator("#bench-stop").click()
                deadline_run = time.monotonic() + 420
                while await page.locator("#bench-export").is_disabled():
                    assert time.monotonic() < deadline_run, "Cloud run/archive timed out"
                    await asyncio.sleep(20)
                    job = await asyncio.to_thread(read_job, run_id)
                    print(name, job["state"], job.get("metrics", {}).get("completed"),
                          job.get("metrics", {}).get("p50_ms"), flush=True)
                if mode == "slow":
                    await cdp.send("Network.emulateNetworkConditions", {
                        "offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
                    evidence["checks"]["slow_browser"] = {
                        "run": run_id, "upload_bytes_per_second": 16384, "added_latency_ms": 300}
                async with page.expect_download(timeout=300000) as event:
                    await page.locator("#bench-export").click()
                download = await event.value
                temp = ROOT / ".state" / (name + ".json")
                await download.save_as(str(temp))
                report = json.loads(temp.read_text())
                check_metrics(report)
                assert report["execution"] == "cloud"
                assert len(report["questions"]) == 360
                path = OUT / (name + ".json.gz")
                path.write_bytes(gzip.compress(temp.read_bytes(), compresslevel=6, mtime=0))
                temp.unlink()
                metadata = await (await context.request.get(base + "/api/history/" + run_id)).json()
                assert metadata["origin"] == "cloud" and metadata["metrics"] == report["metrics"]
                m = report["metrics"]
                evidence["runs"].append({"name": name, "id": run_id, "file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "metrics": m})
                save()
                print(name, json.dumps({k: m[k] for k in (
                    "completed", "successful", "failed", "steady_valid_rps", "p50_ms", "p95_ms",
                    "max_send_lag_ms", "response_accuracy", "passes_performance_acceptance")}), flush=True)
                await expect(page.locator("#bench-rate")).to_be_enabled(timeout=60000)
                # Reload ends only the browser lease. Acquire it again for the next run.
                assert (await context.request.post(base + "/api/risk/start", data={})).status == 200
                await page.evaluate("() => refresh()")
                await expect(page.locator("#bench-run")).to_be_enabled(timeout=60000)
                return report

            warm = await run("risk-warmup", 20, 20)
            assert warm["metrics"]["failed"] == 0
            for round_, mode in ((1, "slow"), (2, "offline")):
                report = await run(f"risk-100rps-120s-r{round_}", 100, 120, mode)
                assert report["metrics"]["passes_performance_acceptance"], report["metrics"]
                assert report["metrics"]["accuracy"] >= .9
            await page.locator('[aria-labelledby="benchmark-title"]').screenshot(path=str(OUT / "risk-cloud.png"))
            stopped = await run("risk-stopped", 100, 120, "stop")
            assert stopped["metrics"]["stopped"] and 0 < stopped["metrics"]["completed"] < 12000
            assert stopped["metrics"]["unsent"] > 0
            evidence["checks"].update(no_browser_inference_requests=not browser_infer,
                                      original_token_hashes_verified=True, browser_errors=errors,
                                      stop_preserves_partial_history=True)
            assert not browser_infer and not errors, (browser_infer, errors)
            evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
            save()
        finally:
            await context.set_offline(False)
            if acquired:
                await context.request.post(base + "/api/risk/end", data={})
            await context.storage_state(path=str(session))
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
