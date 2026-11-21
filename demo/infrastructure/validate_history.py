"""Validate deployed history/recovery/auth in real Chromium, without starting GPUs.

Test entries are explicitly labeled storage checks. Actual inference calls use
invalid inputs and must return 422 before readiness/model routing.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "operations/demo-history-20261121"


async def main():
    state = json.loads((ROOT/"operations/demo-20261121/resources.json").read_text())
    base = "https://"+state["domain"]
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "url": base,
              "scope": "History storage only; no GPU startup; invalid inference requests return HTTP422",
              "runs": [], "checks": {}}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1560, "height": 1100})
        starts = []
        context.on("request", lambda r: starts.append(r.url) if r.method == "POST" and r.url.endswith("/start") else None)
        credentials = json.loads((ROOT/".state/demo-login.json").read_text())
        assert (await context.request.get(base+"/api/history")).status == 401
        assert (await context.request.post(base+"/api/auth", data=credentials)).status == 200
        before = await (await context.request.get(base+"/api/status")).json()
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(base, wait_until="networkidle")
        await expect(page.locator("#login")).not_to_be_visible()
        await expect(page.locator("#history-sync")).to_contain_text("自动保存", timeout=60000)
        for name in ("index.html", "app.js", "history.js", "history-worker.js", "rate-client.js", "style.css"):
            raw = await (await context.request.get(base+"/"+name)).body()
            assert raw == (ROOT/"demo/frontend"/name).read_bytes(), name
        result["checks"]["deployed_files_match"] = True
        # Exercise the normal form handler, worker, outbox and cloud persistence.
        await page.evaluate("""() => {
          group="risk";
          $("bench-execution").value="browser";
          benchmarkQuestions.risk=[{id:"storage-invalid",title:"存储验证：无效输入，非模型性能测试",
            request:{state:{},questions:{}},selected:true}];
          datasetSelections.risk="storage-validation";
          statusData.groups.risk.phase="ready";statusData.groups.risk.session_active=true;
          $("bench-rate").value="5";$("bench-seconds").value="1";renderBenchmarkQuestions();renderStatus();
        }""")
        await page.locator("#bench-run").click()
        await expect(page.locator("#history-sync")).to_contain_text("完整测试记录已保存", timeout=60000)
        rows = await (await context.request.get(base+"/api/history")).json()
        run = next(r for r in rows["items"] if r["origin"] == "demo" and r.get("dataset_id") == "storage-validation")
        assert run["status"] == "completed" and run["metrics"]["completed"] == 5 and run["metrics"]["failed"] == 5
        result["runs"].append(run["id"])
        detail = await (await context.request.get(base+"/api/history/"+run["id"])).json()
        parts = await (await context.request.get(base+f"/api/history/{run['id']}/parts?kind=r")).json()
        records = []
        for part in parts["items"]:
            records.extend(await (await context.request.get(base+f"/api/history/{run['id']}/parts/{part['part']}")).json())
        assert len(records) == 5 and all(r["status"] == 422 and not r["ok"] for r in records)
        result["checks"]["all_failures_saved"] = True

        begin = """async title => {
          const q={id:"storage-invalid",title,request:{state:{},questions:{}},request_json:'{"state":{},"questions":{}}'};
          const id=await HistoryUI.begin({scenario:"risk",kind:"single",dataset_id:"storage-validation",
            questions:[q],settings:{validation_only:true},title});
          const started=performance.now();
          const response=await fetch("/api/risk/infer",{method:"POST",headers:{"Content-Type":"application/json"},body:q.request_json});
          const error=await response.json();
          if(response.status!==422)throw Error("Expected invalid input to be rejected before GPU");
          const metrics={completed:1,successful:0,failed:1,p50_ms:null};
          await HistoryUI.append(id,[{index:1,question_index:0,title,ok:false,status:422,error:error.detail,elapsed_ms:performance.now()-started}],metrics);
          return {id,metrics};
        }"""
        pending = await page.evaluate(begin, "存储验证：刷新恢复，非模型测试")
        # Another tab must not finalize a running tab's outbox.
        tab = await context.new_page()
        await tab.goto(base, wait_until="networkidle")
        await expect(tab.locator("#history-sync")).to_contain_text("自动保存", timeout=60000)
        doc = await (await context.request.get(base+"/api/history/"+pending["id"])).json()
        assert doc["status"] == "running"
        await tab.close()
        await page.reload(wait_until="networkidle")
        await expect(page.locator("#history-sync")).to_contain_text("已恢复并保存 1", timeout=60000)
        doc = await (await context.request.get(base+"/api/history/"+pending["id"])).json()
        assert doc["status"] == "interrupted" and doc["record_count"] == 1
        result["runs"].append(pending["id"])
        result["checks"]["refresh_recovery_and_tab_isolation"] = True

        offline = await page.evaluate(begin, "存储验证：断网补传，非模型测试")
        await context.set_offline(True)
        failed = await page.evaluate("""async r => {
          try {await HistoryUI.finish(r.id,"failed",r.metrics,"无效输入422");return false;}catch{return true;}
        }""", offline)
        assert failed
        await context.set_offline(False)
        await page.reload(wait_until="networkidle")
        await expect(page.locator("#history-sync")).to_contain_text("已恢复并保存 1", timeout=60000)
        doc = await (await context.request.get(base+"/api/history/"+offline["id"])).json()
        assert doc["status"] == "failed" and doc["record_count"] == 1
        result["runs"].append(offline["id"])
        result["checks"]["offline_outbox_recovery"] = True

        stopped = await page.evaluate("""async () => {
          const questions=[{id:"invalid",title:"存储验证：提前停止，非模型测试",request:{state:{},questions:{}}}];
          const id=await HistoryUI.begin({scenario:"risk",kind:"fixed-rps",dataset_id:"storage-validation",questions,
            settings:{rate:5,seconds:4,validation_only:true},title:questions[0].title});
          const runner=new RateClient({questions,scenario:"risk",rate:5,seconds:4,onUpdate:()=>{},
            onRecords:(records,metrics)=>HistoryUI.append(id,records,metrics).catch(()=>{})});
          setTimeout(()=>runner.stop(),500);const m=await runner.run();
          await HistoryUI.finish(id,"stopped",m);return {id,metrics:m};
        }""")
        assert stopped["metrics"]["unsent"] > 0 and stopped["metrics"]["failed"] == stopped["metrics"]["completed"]
        result["runs"].append(stopped["id"])
        result["checks"]["early_stop_preserved"] = True

        # A separate signed-in browser sees the same cloud history.
        second = await browser.new_context(viewport={"width": 390, "height": 844})
        second.on("request", lambda r: starts.append(r.url) if r.method == "POST" and r.url.endswith("/start") else None)
        assert (await second.request.post(base+"/api/auth", data=credentials)).status == 200
        other = await second.new_page()
        await other.goto(base, wait_until="networkidle")
        await expect(other.locator("#history-count")).to_contain_text("轮", timeout=60000)
        await other.locator("#history-origin").select_option("demo")
        await other.locator("#history-search").fill("提前停止")
        await expect(other.locator("#history-results")).to_contain_text("提前停止")
        await other.locator("#history-results button").first.click()
        await expect(other.locator("#history-records")).to_contain_text("存储验证：提前停止")
        await other.locator("#history-records button").first.click()
        await expect(other.locator("#history-request-json")).to_contain_text('"status": 422')
        async with other.expect_download() as download:
            await other.locator("#history-export").click()
        exported = json.loads(Path(await (await download.value).path()).read_text())
        assert len(exported["records"]) == stopped["metrics"]["completed"]
        assert await other.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await other.screenshot(path=str(OUT/"history-mobile.png"), full_page=True)
        result["checks"]["cross_browser_detail_export_mobile"] = True
        anonymous = await p.request.new_context()
        for path in ("/api/history/"+run["id"], f"/api/history/{run['id']}/parts",
                     f"/api/history/{run['id']}/parts/q000000"):
            assert (await anonymous.get(base+path)).status == 401
        assert (await anonymous.post(base+"/api/history", data={"id":"test"})).status == 401
        await anonymous.dispose()
        after = await (await context.request.get(base+"/api/status")).json()
        result["gpu_status_observed"] = {
            g: {"before": before["groups"][g]["phase"], "after": after["groups"][g]["phase"]}
            for g in ("risk", "sop")}
        # Existing presenters may have running GPU leases. This test acquires
        # none and must never assume unrelated groups have stopped.
        result["checks"]["no_gpu_start"] = not starts and all(
            not before["groups"][g]["session_active"] and not after["groups"][g]["session_active"]
            for g in ("risk", "sop"))
        assert result["checks"]["no_gpu_start"], (before, after)
        result["checks"]["no_javascript_errors"] = not errors
        assert not errors, errors
        result["checks"]["authentication_required"] = True
        await page.screenshot(path=str(OUT/"history-desktop.png"), full_page=True)
        await browser.close()
    result["passed"] = True
    (OUT/"validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
