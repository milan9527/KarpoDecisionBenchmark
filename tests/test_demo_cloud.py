import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"demo/backend"))
from cloud_jobs import Jobs, select_questions
from cloud_metrics import summarize
from cloud_worker import Runner, archive
from history import History
from registry import Registry
from state import Conflict
from test_demo_history import Table, S3


class CloudJobsTests(unittest.TestCase):
    def test_exclusive_jobs_and_explicit_stop(self):
        jobs = Jobs(Table(), S3(), "private")
        with self.assertRaises(Conflict):
            jobs.reserve("run-a")
        jobs.heartbeat()
        jobs.reserve("run-a")
        with self.assertRaises(Conflict):
            jobs.reserve("run-b")
        jobs.create("run-a", "risk", "session", {"rate": 100, "seconds": 120}, {"questions": []})
        self.assertNotIn("session", jobs.public(jobs.active()))
        self.assertTrue(jobs.stop("run-a")["stop_requested"])
        jobs.release("different-run")
        with self.assertRaises(Conflict):
            jobs.reserve("run-b")
        jobs.release("run-a")
        jobs.update("run-a", state="completed")
        jobs.reserve("run-b")

    def test_expired_lock_cannot_abandon_an_unarchived_job(self):
        jobs = Jobs(Table(), S3(), "private")
        jobs.heartbeat()
        jobs.reserve("run-a")
        jobs.create("run-a", "risk", "session", {}, {"questions": []})
        jobs.change("active", lambda old: {**old, "expires": 0})
        with self.assertRaises(Conflict):
            jobs.reserve("run-b")

    def test_recovery_archive_is_idempotent_and_browser_cannot_overwrite(self):
        table, s3 = Table(), S3()
        history = History(table, s3, "private")
        run = "cloud_recovery_test_000001"
        history.create({"id": run, "scenario": "risk", "kind": "fixed-rps"}, "cloud-worker", True)
        history.all_parts = lambda run, kind=None: (
            json.loads(item["document"]) for (pk, sk), item in table.rows.items() if pk == run)
        definition = {"questions": [{"id": "q", "request_json": '{"value":0.0}'}]}
        metrics = {"completed": 1, "failed": 1, "unsent": 11999}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "requests.jsonl").write_text('{"index":1,"ok":false,"status":429}\n{"index":')
            archive(history, {"id": run}, definition, path, metrics, "interrupted")
            archive(history, {"id": run}, definition, path, metrics, "interrupted")
        doc = history.get(run)
        self.assertEqual(doc["status"], "interrupted")
        self.assertEqual(doc["record_count"], 1)
        self.assertEqual(doc["origin"], "cloud")
        self.assertEqual(len(s3.objects), 2)
        with self.assertRaises(ValueError):
            history.finish(run, {"metrics": metrics, "status": "completed"}, "demo")

    def test_selection_preserves_original_wire_input_and_custom_cannot_supply_expected(self):
        registry = Registry(Path(__file__).resolve().parents[1])
        q = registry.datasets["promo-tune"]["questions"][0]
        body = {"dataset_id": "promo-tune", "rate": 100, "seconds": 120, "question_ids": [q["id"]],
                "custom": [{"title": "custom", "request_json": q["request_json"], "expected": "deny"}]}
        questions = select_questions(registry, "risk", body)
        self.assertEqual(questions[0]["request_json"], q["request_json"])
        self.assertEqual(questions[1]["request_json"], q["request_json"])
        self.assertNotIn("expected", questions[1])
        body["question_ids"] *= 2
        with self.assertRaises(ValueError):
            select_questions(registry, "risk", body)
        body["question_ids"] = [q["id"]]
        with self.assertRaises(ValueError):
            select_questions(registry, "sop", body)

    def test_all_failures_remain_in_full_accuracy_and_formal_acceptance(self):
        qs = [{"expected": "allow"}, {}]
        rows = []
        for i in range(12000):
            rows.append({"index": i+1, "question_index": i%2, "ok": True, "correct": i%2 == 0,
                         "sent": True, "send_offset_s": i/100, "scheduled_offset_s": i/100,
                         "end_offset_s": i/100+.05, "elapsed_ms": 50, "http_ms": 50, "send_lag_ms": 0,
                         "response": {"demo": {"service_ms": 40}, "metrics": {"engine_wait_and_model_ms": 30}}})
        m = summarize(rows, qs, 100, 120, 120.04, 12000, 5, finished=True)
        self.assertTrue(m["passes_performance_acceptance"])
        rows[100].update(ok=False, correct=False)
        m = summarize(rows, qs, 100, 120, 120.04, 12000, 5, finished=True)
        self.assertEqual(m["response_accuracy"], 1)
        self.assertEqual(m["accuracy"], 5999/6000)
        self.assertEqual(m["failed"], 1)
        self.assertFalse(m["passes_performance_acceptance"])


class CloudRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_loop_no_retries_exact_inputs_and_every_attempt_on_disk(self):
        calls = []
        async def send(request):
            calls.append(request.content)
            n = len(calls)
            await asyncio.sleep(.04)
            if n % 3 == 0:
                return httpx.Response(500, json={"detail": "intentional test failure"})
            return httpx.Response(200, json={
                "answers": {"action": {"choice": "allow", "probabilities": {"allow": .9, "deny": .1}}},
                "demo": {"live_model": True, "service_ms": 30},
                "metrics": {"engine_wait_and_model_ms": 20}})
        q = {"id": "q", "title": "Q", "expected": "allow",
             "request": {"state": {"ratio": 0.0}, "questions": {"action": {"criteria": {"allow": "", "deny": ""}}}}}
        q["request_json"] = json.dumps(q["request"])
        job = {"id": "test", "scenario": "risk", "deadline": time.time()+30,
               "settings": {"rate": 100, "seconds": 1}}
        jobs = Jobs(Table(), S3(), "private")
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(send)) as client:
                runner = Runner(client, job, [q], Path(directory), jobs, asyncio.Event())
                m = await runner.run()
            records = [json.loads(l) for l in (Path(directory)/"requests.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 100)
        self.assertEqual(len(records), 100)
        self.assertEqual(m["failed"], 33)
        self.assertEqual(m["successful"], 67)
        self.assertGreater(m["peak_concurrency"], 1)
        self.assertTrue(all(raw == q["request_json"].encode() for raw in calls))
        self.assertEqual(sorted(r["scheduled_offset_s"] for r in records), [i/100 for i in range(100)])
        self.assertEqual(m["response_accuracy"], 1)
        self.assertEqual(m["accuracy"], .67)

    async def test_stop_before_dispatch_retains_empty_run(self):
        async with httpx.AsyncClient(base_url="https://example.test") as client:
            job = {"id": "stop", "scenario": "risk", "stop_requested": True,
                   "settings": {"rate": 100, "seconds": 120}}
            with tempfile.TemporaryDirectory() as directory:
                runner = Runner(client, job, [{"request_json": "{}", "title": "Q"}],
                                Path(directory), None, asyncio.Event())
                m = await runner.run()
        self.assertEqual(m["completed"], 0)
        self.assertEqual(m["unsent"], 12000)
        self.assertTrue(m["stopped"])


if __name__ == "__main__":
    unittest.main()
