"""Verify deployed username/password authentication without starting any GPU.

Requires Playwright + Chromium. Does not record credentials or cookies.
This test consumes the login rate limit; wait a minute before running it again.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-auth-20261121"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    resources = json.loads((ROOT / "operations/demo-20261121/resources.json").read_text())
    credentials = json.loads((ROOT / ".state/demo-login.json").read_text())
    base = "https://" + resources["domain"]
    checks = {}
    errors, gpu_starts = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 1050})
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: gpu_starts.append(request.url) if request.url.endswith("/start") else None)
        anonymous = await playwright.request.new_context(base_url=base)
        for path in ("status", "catalog"):
            assert (await anonymous.get("/api/" + path)).status == 401
        for group in ("risk", "sop"):
            for action in ("start", "activity", "end", "infer"):
                assert (await anonymous.post(f"/api/{group}/{action}", data={})).status == 401
        checks["anonymous_protected_routes"] = "10/10 return 401"
        for filename in ("index.html", "app.js", "benchmark.js", "style.css"):
            response = await anonymous.get("/" + filename)
            content = await response.body()
            assert content == (ROOT / "demo/frontend" / filename).read_bytes(), filename
            assert credentials["password"].encode() not in content
        checks["deployed_frontend_matches_local"] = True
        await page.goto(base, wait_until="networkidle")
        await expect(page.locator("#login")).to_be_visible()
        await page.screenshot(path=str(OUT / "login-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.screenshot(path=str(OUT / "login-mobile.png"), full_page=True)
        checks["mobile_no_horizontal_overflow"] = True
        await page.set_viewport_size({"width": 1440, "height": 1050})
        await page.get_by_label("用户名", exact=True).fill(credentials["username"])
        await page.get_by_label("密码", exact=True).fill("incorrect-password")
        await page.locator("#login-button").click()
        await expect(page.locator("#login-error")).to_have_text("用户名或密码不正确")
        await expect(page.locator("#login")).to_be_visible()
        checks["wrong_password_stays_on_login"] = True
        await page.locator("#password").fill(credentials["password"])
        await page.locator("#login-button").click()
        await page.locator("#login").wait_for(state="hidden")
        await expect(page.locator("#password")).to_have_value("")
        cookie = next(c for c in await context.cookies() if c["name"] == "karpo_demo")
        assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Strict"
        assert "karpo_demo" not in await page.evaluate("document.cookie")
        checks["cookie_secure_httponly_samesite_strict"] = True
        checks["valid_username_password_login"] = True
        before = await (await context.request.get(base + "/api/status")).json()
        for group in ("risk", "sop"):
            assert before["groups"][group]["phase"] == "stopped"
            assert before["groups"][group]["session_active"] is False
            await page.locator(f".tab[data-group={group}]").click()
            await expect(page.locator("#start")).to_be_enabled()
            await expect(page.locator("#infer")).to_be_disabled()
        await page.reload(wait_until="networkidle")
        await expect(page.locator("#login")).not_to_be_visible()
        await expect(page.locator("#start")).to_be_enabled()
        checks["reload_preserves_authenticated_session"] = True
        after = await (await context.request.get(base + "/api/status")).json()
        assert before["groups"] == after["groups"]
        assert not gpu_starts
        checks["login_does_not_acquire_gpu_session"] = True
        checks["browser_gpu_start_requests"] = len(gpu_starts)
        await context.clear_cookies()
        await page.reload(wait_until="networkidle")
        await expect(page.locator("#login")).to_be_visible()
        checks["missing_cookie_requires_login_again"] = True
        modified = cookie["value"][:-1] + ("0" if cookie["value"][-1] != "0" else "1")
        tampered = await anonymous.get("/api/status", headers={"Cookie": "karpo_demo=" + modified})
        assert tampered.status == 401
        checks["tampered_cookie"] = 401
        failures = [
            {"username": "nonexistent-account", "password": credentials["password"]},
            {"code": "legacy-access-code"},
            {"username": "demo", "password": ["invalid-type"]},
            {"username": "demo", "password": "x" * 1025},
        ]
        for payload in failures:
            response = await anonymous.post("/api/auth", data=payload)
            assert response.status == 401, response.status
            assert (await response.json())["detail"] == "用户名或密码不正确"
        checks["unknown_account_legacy_code_invalid_types_oversize"] = "all 401 with same error"
        # Viewer-supplied XFF prefixes must not bypass the CloudFront/ALB limit.
        statuses = []
        for i in range(10):
            response = await anonymous.post("/api/auth", data={"username": "missing", "password": "wrong"},
                                            headers={"X-Forwarded-For": f"192.0.2.{i + 1}"})
            statuses.append(response.status)
            assert response.status in (401, 429)
            if response.status == 429:
                assert response.headers["retry-after"] == "60"
                break
        assert 429 in statuses
        checks["rate_limit_with_spoofed_forwarded_for"] = statuses
        assert not errors, errors
        await anonymous.dispose()
        await context.close()
        await browser.close()
    report = {"at": datetime.now(timezone.utc).isoformat(), "url": base, "passed": True,
              "checks": checks, "gpu_control_status": after, "page_errors": errors,
              "frontend_sha256": {name: hashlib.sha256((ROOT / "demo/frontend" / name).read_bytes()).hexdigest()
                                  for name in ("index.html", "app.js", "benchmark.js", "style.css")}}
    (OUT / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("Username/password browser and API validation passed. No authenticated GPU start requests sent.")


if __name__ == "__main__":
    asyncio.run(main())
