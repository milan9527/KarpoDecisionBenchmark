"""Historical closed-loop UI validation (commit 47ff421).

The current fixed-RPS UI is validated by validate_rps.py.

Starts/renews the two demo sessions, then releases this browser's leases even on
failure. The independent watchdog retains the normal ten-minute shutdown rule.
Requires Playwright/Chromium; outputs contain model examples but no credentials.
"""
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-performance-20261121"


def check_metrics(report):
    records, m = report["records"], report["metrics"]
    good = [r for r in records if r["ok"]]
    assert m["completed"] == len(records) == m["started"]
    assert m["successful"] == len(good)
    assert m["failed"] == len(records) - len(good)
    assert m["peak_concurrency"] <= m["configured_concurrency"]
    assert m["active"] == 0
    assert len(set(r["index"] for r in records)) == len(records)
    if records:
        assert math.isclose(m["success_rate"], len(good) / len(records))
    assert math.isclose(m["successful_rps"], len(good) * 1000 / m["duration_ms"])
    times = sorted(r["elapsed_ms"] for r in good)
    for name, p in [("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99)]:
        assert m[name] == (times[math.ceil(len(times) * p) - 1] if times else None)
    for record in good:
        assert record["response"]["demo"]["live_model"] is True


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    resources = json.loads((ROOT / "operations/demo-20261121/resources.json").read_text())
    base = "https://" + resources["domain"]
    evidence = {"url": base, "started_at": datetime.now(timezone.utc).isoformat(),
                "scope": "Public browser functional validation; finite closed-loop runs, not a 100 RPS SLA benchmark",
                "runs": [], "checks": {}}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        session_file = ROOT / ".state/demo-performance-session.json"
        context = await browser.new_context(viewport={"width": 1600, "height": 1100},
                                            **({"storage_state": str(session_file)} if session_file.exists() else {}))
        page = await context.new_page()
        errors, sent = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: sent.append(request.post_data_json)
                if request.url.endswith("/infer") else None)
        try:
            if (await context.request.get(base + "/api/catalog")).status == 401:
                credentials = json.loads((ROOT / ".state/demo-login.json").read_text())
                response = await context.request.post(base + "/api/auth", data=credentials)
                assert response.status == 200
            for group in ("risk", "sop"):
                response = await context.request.post(base + f"/api/{group}/start", data={})
                assert response.status == 200
            await page.goto(base, wait_until="networkidle")
            for filename in ("index.html", "app.js", "benchmark.js", "style.css"):
                response = await context.request.get(base + "/" + filename)
                assert await response.body() == (ROOT / "demo/frontend" / filename).read_bytes(), filename
            evidence["checks"]["deployed_frontend_matches"] = True
            catalog = await (await context.request.get(base + "/api/catalog")).json()

            async def run(name, concurrency, total, stop=False):
                sent.clear()
                await page.locator("#bench-concurrency").select_option(str(concurrency))
                await page.locator("#bench-total").fill(str(total))
                await page.locator("#bench-run").click()
                if stop:
                    await expect(page.locator("#bench-stop")).to_be_enabled()
                    await page.locator("#bench-stop").click()
                await expect(page.locator("#bench-export")).to_be_enabled(timeout=120000)
                async with page.expect_download() as download_event:
                    await page.locator("#bench-export").click()
                download = await download_event.value
                await download.save_as(str(OUT / (name + ".json")))
                report = json.loads((OUT / (name + ".json")).read_text())
                check_metrics(report)
                assert len(sent) == report["metrics"]["started"]
                for index, payload in enumerate(sent):
                    assert payload == report["questions"][index % len(report["questions"])]["request"]
                assert (await page.locator("#bench-rps").inner_text()) == f'{report["metrics"]["successful_rps"]:.1f}'
                if report["metrics"]["p50_ms"] is not None:
                    assert (await page.locator("#bench-p50").inner_text()) == f'{report["metrics"]["p50_ms"]:.1f}'
                evidence["runs"].append({"name": name, "file": name + ".json", "metrics": report["metrics"]})
                print(name, json.dumps(report["metrics"]), flush=True)
                return report

            for group in ("risk", "sop"):
                await page.locator(f".tab[data-group={group}]").click()
                deadline = time.monotonic() + 1200
                while await page.locator("#bench-run").is_disabled():
                    if time.monotonic() > deadline:
                        raise AssertionError("Readiness timed out: " + group)
                    await asyncio.sleep(5)
                    state = await (await context.request.get(base + "/api/status")).json()
                    for active_group in ("risk", "sop"):
                        if state["groups"][active_group]["session_active"]:
                            await context.request.post(base + f"/api/{active_group}/activity", data={})
                    if state["groups"][group].get("error"):
                        raise AssertionError(state["groups"][group]["error"])
                print(group, "ready", flush=True)
                single = await run(group + "-c1", 1, 3)
                assert single["metrics"]["successful"] == 3
                await page.locator("#state").fill(json.dumps({
                    **json.loads(await page.locator("#state").input_value()),
                    "user_message" if group == "risk" else "customer_message": "请根据系统中的可信事实判断本次操作。",
                }, ensure_ascii=False))
                await page.locator("#bench-add").click()
                await expect(page.locator("#bench-questions input")).to_have_count(4)
                custom = await run(group + "-c4-custom", 4, 12)
                assert custom["metrics"]["successful"] == 12
                assert len(custom["questions"]) == 4
                await page.locator("#bench-questions button[aria-label^=移除]").click()
                hundred = await run(group + "-c100", 100, 300)
                assert hundred["metrics"]["successful"] == 300, hundred["metrics"]
                assert hundred["metrics"]["peak_concurrency"] == 100
                assert [q["request"] for q in hundred["questions"]] == [q["request"] for q in catalog[group]["examples"]]
                expected = ["review", "allow", "deny"] if group == "risk" else ["sop_048", "sop_017", "sop_157"]
                for record in hundred["records"]:
                    response = record["response"]
                    actual = response.get("decision", {}).get("action") or response["answers"]["action"]["choice"]
                    assert actual == expected[record["question_index"]], (group, actual)
                await page.locator("#bench-results .question-edit").first.click()
                await expect(page.locator("#bench-detail")).to_be_visible()
                detail = json.loads(await page.locator("#bench-detail-json").text_content())
                assert "request" in detail and "response" in detail
                await page.locator("#bench-detail summary").click()
                await page.locator(".benchmark-panel").screenshot(path=str(OUT / (group + "-performance.png")))
                if group == "risk":
                    stopped = await run("risk-stopped", 4, 1000, stop=True)
                    assert 0 < stopped["metrics"]["started"] < 1000
                    assert stopped["metrics"]["unsent"] > 0
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await page.locator(".benchmark-panel").screenshot(path=str(OUT / (group + "-mobile.png")))
                await page.set_viewport_size({"width": 1600, "height": 1100})
            # Submit an invalid custom question: errors must be counted, never hidden or retried.
            await page.locator("#state").fill('{"customer_message":"missing trusted facts"}')
            await page.locator("#bench-add").click()
            for checkbox in await page.locator("#bench-questions input").all():
                await checkbox.uncheck()
            await page.locator("#bench-questions input").last.check()
            failed = await run("sop-invalid-input", 2, 2)
            assert failed["metrics"]["successful"] == 0 and failed["metrics"]["failed"] == 2
            assert all(r["status"] == 422 for r in failed["records"])
            evidence["checks"].update(no_browser_errors=not errors, mobile_no_overflow=True,
                original_inputs_preserved=True, custom_questions=True, stop_drains_inflight=True,
                invalid_requests_counted=True, exported_metrics_independently_recomputed=True,
                risk_c100_expected_actions=300, sop_c100_expected_actions=300)
            assert not errors, errors
        finally:
            # Release only this login session; other presenters retain their own leases.
            for group in ("risk", "sop"):
                response = await context.request.post(base + f"/api/{group}/end", data={})
                assert response.status == 200
            evidence["after_end"] = await (await context.request.get(base + "/api/status")).json()
            evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
            evidence["page_errors"] = errors
            (OUT / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            await context.close()
            await browser.close()
    print("Real GPU performance UI validation passed; this test's sessions ended.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
