"""Run actual public frontend inference, then arm the normal ten-minute shutdown.

Requires playwright + Chromium. Uses the untracked local username/password; never
records credentials, cookies or headers. This is a functional demo validation,
not a load/quality benchmark.
"""
import asyncio
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-20261121"


async def main(stopped_only=False):
    resources = json.loads((OUT / "resources.json").read_text())
    credentials = json.loads((ROOT / ".state/demo-login.json").read_text())
    base = "https://" + resources["domain"]
    expected = {"risk": ["review", "allow", "deny"], "sop": ["sop_048", "sop_017", "sop_157"]}
    report = {"url": base, "started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "actual browser smoke test; not a load benchmark", "cases": [], "checks": {}}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 1100}, device_scale_factor=1)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        status = await context.request.get(base + "/api/status")
        assert status.status == 401, (status.status, await status.text())
        report["checks"]["unauthenticated_api"] = status.status
        response = await page.goto(base, wait_until="networkidle")
        assert response.status == 200
        report["checks"]["frontend"] = response.status
        report["checks"]["csp"] = response.headers.get("content-security-policy")
        await page.locator("#username").fill(credentials["username"])
        await page.locator("#password").fill(credentials["password"])
        await page.locator("#login-button").click()
        await page.locator("#login").wait_for(state="hidden")
        catalog_response = await context.request.get(base + "/api/catalog")
        catalog = await catalog_response.json()
        if stopped_only:
            status = await (await context.request.get(base + "/api/status")).json()
            for group in ("risk", "sop"):
                assert status["groups"][group]["phase"] == "stopped", status
                assert status["groups"][group]["ready_gpus"] == 0
                await page.locator(f".tab[data-group={group}]").click()
                await expect(page.locator("#status-title")).to_have_text("GPU 尚未启动", timeout=10000)
                await expect(page.locator("#infer")).to_be_disabled()
                await expect(page.locator("#start")).to_be_enabled()
            assert not errors, errors
            await page.screenshot(path=str(OUT / "frontend-stopped.png"), full_page=True)
            (OUT / "post-shutdown-frontend.json").write_text(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(), "url": base, "passed": True,
                "checks": report["checks"], "status": status, "page_errors": errors,
                "start_buttons_enabled": True, "infer_buttons_disabled": True,
                "gpu_start_requests_sent": 0,
            }, ensure_ascii=False, indent=2) + "\n")
            await context.close()
            await browser.close()
            print("GPU stopped: frontend/API available, Start enabled, inference disabled.", flush=True)
            return
        for group in ("risk", "sop"):
            await page.locator(f".tab[data-group={group}]").click()
            gated = await context.request.post(base + f"/api/{group}/infer",
                                              data=catalog[group]["examples"][0]["request"])
            assert gated.status == 409, (group, gated.status, await gated.text())
            report["checks"][group + "_before_session"] = gated.status
            await page.locator("#start").click()
            until = time.monotonic() + 1200
            while await page.locator("#infer").is_disabled():
                if time.monotonic() > until:
                    raise AssertionError("GPU readiness timeout: " + group)
                error = await page.locator("#status-error").text_content()
                if error:
                    raise AssertionError(error)
                await asyncio.sleep(5)
            print(group, "all replicas ready", flush=True)
            for index, action in enumerate(expected[group]):
                await page.locator("#examples button").nth(index).click()
                async with page.expect_response(lambda r: r.url.endswith(f"/api/{group}/infer")) as event:
                    await page.locator("#infer").click()
                response = await event.value
                result = await response.json()
                assert response.status == 200, result
                assert result["demo"]["live_model"] is True
                actual = result.get("decision", {}).get("action") or result["answers"]["action"]["choice"]
                assert actual == action, (group, index, actual, action)
                await expect(page.locator("#infer")).to_be_enabled()
                request = response.request.post_data_json
                assert request["questions"] == catalog[group]["examples"][index]["request"]["questions"]
                assert request["state"] == catalog[group]["examples"][index]["request"]["state"]
                if group == "risk":
                    assert sum(len(v) for v in request["state"]["trusted_features"].values()) == 200
                else:
                    assert len(request["questions"]["action"]["criteria"]) == 200
                record = {"scenario": group, "example": catalog[group]["examples"][index]["title"],
                          "request": request, "response": result, "http_status": response.status,
                          "browser_latency_label": await page.locator("#latency").text_content(),
                          "expected_action": action, "passed": True}
                report["cases"].append(record)
                print(group, record["example"], actual, record["browser_latency_label"], flush=True)
                if index == 0:
                    await page.screenshot(path=str(OUT / f"frontend-{group}.png"), full_page=True)
            invalid = json.loads(json.dumps(catalog[group]["examples"][0]["request"]))
            invalid["questions"] = {}
            bad = await context.request.post(base + f"/api/{group}/infer", data=invalid)
            assert bad.status == 422
            report["checks"][group + "_changed_policy"] = bad.status
        malformed = await context.request.post(base + "/api/risk/infer",
            data="{", headers={"Content-Type": "application/json"})
        assert malformed.status == 422
        report["checks"]["invalid_json"] = malformed.status
        cross = await context.request.post(base + "/api/risk/activity",
            data={}, headers={"Origin": "https://invalid.example"})
        assert cross.status == 403
        report["checks"]["cross_origin"] = cross.status
        status_response = await context.request.get(base + "/api/status")
        assert "no-store" in status_response.headers["cache-control"]
        report["checks"]["api_cache_control"] = status_response.headers["cache-control"]
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.screenshot(path=str(OUT / "frontend-mobile.png"), full_page=True)
        report["checks"]["mobile_no_horizontal_overflow"] = True
        await page.set_viewport_size({"width": 1600, "height": 1100})
        await page.locator("#end-all").click()
        await expect(page.locator("#end-all")).to_be_disabled()
        # Wait for both POSTs and the post-end status refresh to complete.
        await asyncio.sleep(2)
        after = await (await context.request.get(base + "/api/status")).json()
        report["after_end"] = after
        for group in ("risk", "sop"):
            assert after["groups"][group]["phase"] == "ending", after
            assert after["groups"][group]["session_active"] is False
            assert 590 <= after["groups"][group]["stop_after"] - after["now"] <= 600
            gated = await context.request.post(base + f"/api/{group}/infer",
                                              data=catalog[group]["examples"][0]["request"])
            assert gated.status == 409
            report["checks"][group + "_after_end"] = gated.status
        report["page_errors"] = errors
        assert not errors, errors
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (OUT / "browser-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        await page.screenshot(path=str(OUT / "frontend-ending.png"), full_page=True)
        # Closing the page must not move the already-recorded deadline.
        await context.close()
        await browser.close()
    print("Public browser validation passed; GPU shutdown deadlines recorded.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stopped", action="store_true", help="Check the stopped-GPU UI without starting any GPU")
    asyncio.run(main(parser.parse_args().stopped))
