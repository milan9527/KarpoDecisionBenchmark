"""Independent AWS load generator. Browser traffic never carries the test load."""
import asyncio
import base64
import gc
import gzip
import json
import math
import os
from pathlib import Path
import signal
import time

import boto3
from botocore.config import Config
import httpx

from auth import issue_session
from cloud_jobs import Jobs, ACTIVE
from cloud_metrics import summarize
from history import History, packed
from async_http import Client as AsyncHTTPClient

ROOT = Path(os.environ.get("DEMO_ROOT", "/opt/karpo-demo"))
SPOOL = ROOT / "cloud-runs"
OWNER = "cloud-worker"


def valid_response(response, question):
    if response.get("demo", {}).get("live_model") is not True:
        raise ValueError("响应未标明真实模型推理")
    for name, spec in question["request"]["questions"].items():
        answer = response.get("answers", {}).get(name, {})
        probs = answer.get("probabilities", {})
        if (answer.get("choice") not in spec["criteria"] or set(probs) != set(spec["criteria"])
                or any(not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1 for p in probs.values())
                or abs(sum(probs.values()) - 1) > .01):
            raise ValueError("模型响应格式或候选概率无效")


class Runner:
    def __init__(self, client, job, questions, directory, jobs, stopping):
        self.client, self.job, self.questions = client, job, questions
        self.directory, self.jobs, self.stopping = directory, jobs, stopping
        self.rows, self.started, self.peak, self.active = [], 0, 0, 0
        self.stop_requested = job.get("stop_requested", False)
        self.finished = False
        self.rate, self.seconds = job["settings"]["rate"], job["settings"]["seconds"]
        self.bodies = [q["request_json"].encode() for q in questions]
        self.begin = time.perf_counter() + .1
        self.elapsed = 0
        self.gc_max_pause_ms = 0

    def metrics(self):
        value = summarize(self.rows, self.questions, self.rate, self.seconds,
                         self.elapsed if self.finished else max(0, time.perf_counter() - self.begin),
                         self.started, self.peak, self.stop_requested, self.finished)
        value["generator_gc_max_pause_ms"] = self.gc_max_pause_ms
        return value

    async def call(self, index, stream):
        question_index = index % len(self.questions)
        q, now = self.questions[question_index], time.perf_counter()
        scheduled = self.begin + index / self.rate
        row = {"index": index + 1, "question_index": question_index, "title": q["title"],
               "scheduled_offset_s": index / self.rate, "send_offset_s": now - self.begin,
               "send_lag_ms": max(0, now - scheduled) * 1000, "sent": self.active < 500}
        self.started += 1
        if row["sent"]:
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                response = await self.client.post(f"/api/{self.job['scenario']}/infer",
                                                  content=self.bodies[question_index],
                                                  headers={"Content-Type": "application/json"})
                row["status"] = response.status_code
                value = response.json()
                if response.status_code != 200:
                    row["error"] = str(value.get("detail", "请求失败"))
                    row["ok"] = False
                    if response.status_code in (401, 409):
                        self.stop_requested = True
                else:
                    valid_response(value, q)
                    row.update(ok=True, response=value)
                    if "expected" in q:
                        choice = value.get("decision", {}).get("action") or value["answers"]["action"]["choice"]
                        row["correct"] = choice == q["expected"]
            except Exception as error:
                row.update(ok=False, status=row.get("status", 0), error=f"{type(error).__name__}: {str(error)[:500]}")
            self.active -= 1
        else:
            row.update(ok=False, status=0, error="云端在途请求达到500安全上限，本次计划请求记为失败")
        end = time.perf_counter()
        row.update(end_offset_s=end - self.begin, elapsed_ms=(end - scheduled)*1000, http_ms=(end - now)*1000)
        self.rows.append(row)
        # Appending after timing preserves every completed response on EBS.
        stream.write(packed(row) + "\n")
        stream.flush()

    async def run(self):
        with (self.directory / "requests.jsonl").open("x") as stream:
            async def progress():
                while not self.finished:
                    await asyncio.sleep(2)
                    if self.finished:
                        break
                    latest = await asyncio.to_thread(self.jobs.read, self.job["id"])
                    self.stop_requested |= latest.get("stop_requested", False) or self.stopping.is_set()
                    if time.time() > self.job["deadline"]:
                        self.stop_requested = True
                    # Percentiles grow with the run; keep their sorting and
                    # aggregation off the fixed-arrival event loop.
                    snapshot = (self.rows.copy(), self.questions, self.rate, self.seconds,
                                max(0, time.perf_counter() - self.begin), self.started,
                                self.peak, self.stop_requested, False)
                    metrics = await asyncio.to_thread(summarize, *snapshot)
                    await asyncio.to_thread(self.jobs.update, self.job["id"], metrics=metrics)
                    await asyncio.to_thread(os.fsync, stream.fileno())
            publisher = asyncio.create_task(progress())
            tasks = []
            gc_start = None
            def gc_probe(phase, info):
                nonlocal gc_start
                if phase == "start":
                    gc_start = time.perf_counter()
                elif gc_start is not None and not self.finished and gc_start >= self.begin:
                    self.gc_max_pause_ms = max(self.gc_max_pause_ms, (time.perf_counter() - gc_start) * 1000)
            gc.callbacks.append(gc_probe)
            try:
                for index in range(self.rate * self.seconds):
                    await asyncio.sleep(max(0, self.begin + index/self.rate - time.perf_counter()))
                    if self.stop_requested or self.stopping.is_set():
                        self.stop_requested = True
                        break
                    if index % 100 == 0:
                        # Keep only pending tasks and surface completed errors.
                        # Full responses remain in rows and the durable spool.
                        for task in tasks:
                            if task.done():
                                task.result()
                        tasks = [task for task in tasks if not task.done()]
                    tasks.append(asyncio.create_task(self.call(index, stream)))
                if not self.stop_requested:
                    await asyncio.sleep(max(0, self.begin + self.seconds - time.perf_counter()))
                await asyncio.gather(*tasks)
                self.elapsed = max(0, time.perf_counter() - self.begin)
                self.finished = True
                await asyncio.gather(publisher, return_exceptions=True)
                os.fsync(stream.fileno())
                return self.metrics()
            finally:
                gc.callbacks.remove(gc_probe)
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)
                # On an unexpected spool/progress failure, finish cancellations
                # before closing the file or releasing the GPU lease.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def archive(history, job, definition, directory, metrics, status):
    rows = []
    if (directory / "requests.jsonl").exists():
        with (directory / "requests.jsonl").open() as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # A process crash may leave the final line truncated.
                    break
    rows.sort(key=lambda r: r["index"])
    for kind, values, size in (("q", definition["questions"], 20), ("r", rows, 50)):
        for index in range(0, len(values), size):
            encoded = base64.b64encode(gzip.compress(packed(values[index:index+size]).encode(), mtime=0)).decode()
            history.write_part(job["id"], f"{kind}{index//size:06d}", encoded, OWNER)
    history.finish(job["id"], {"status": status, "metrics": metrics,
                              "question_count": len(definition["questions"])}, OWNER)


async def main():
    resources = json.loads((ROOT / "resources.json").read_text())
    config = json.loads((ROOT / "demo/config.json").read_text())
    sdk = Config(connect_timeout=4, read_timeout=15, retries={"max_attempts": 2})
    region = config["region"]
    table = boto3.resource("dynamodb", region_name=region, config=sdk).Table(resources["history_table"])
    s3 = boto3.client("s3", region_name=region, config=sdk)
    history, jobs = History(table, s3, resources["bucket"]), Jobs(table, s3, resources["bucket"])
    secret = json.loads(boto3.client("secretsmanager", region_name=region, config=sdk).get_secret_value(
        SecretId=resources["backend_secret"])["SecretString"])
    stopping = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopping.set)
    SPOOL.mkdir(exist_ok=True)

    async def heartbeats():
        while not stopping.is_set():
            try:
                await asyncio.to_thread(jobs.heartbeat)
            except Exception as error:
                print("heartbeat", type(error).__name__, flush=True)
            await asyncio.sleep(5)
    heartbeat = asyncio.create_task(heartbeats())
    try:
        while not stopping.is_set():
            job = await asyncio.to_thread(jobs.active)
            if not job or job["state"] not in ACTIVE:
                await asyncio.sleep(1)
                continue
            directory = SPOOL / job["id"]
            directory.mkdir(exist_ok=True)
            definition = await asyncio.to_thread(jobs.definition, job)
            cookie = issue_session(job["session"], secret["cookie_key"], secret["account"]["username"])
            async with AsyncHTTPClient(base_url="https://" + resources["domain"], timeout=65,
                    cookies={"karpo_demo": cookie}, connections=512) as client:
                try:
                    if job["state"] == "queued" and job["deadline"] > time.time():
                        await asyncio.to_thread(jobs.update, job["id"], state="running", worker_started_at=time.time())
                        runner = Runner(client, job, definition["questions"], directory, jobs, stopping)
                        metrics = await runner.run()
                        status = "stopped" if runner.stop_requested else "completed"
                        (directory / "result.json").write_text(packed({"metrics": metrics, "status": status}))
                    elif (directory / "result.json").exists():
                        result = json.loads((directory / "result.json").read_text())
                        metrics, status = result["metrics"], result["status"]
                    else:
                        rows = []
                        if (directory / "requests.jsonl").exists():
                            for line in (directory / "requests.jsonl").read_text().splitlines():
                                try:
                                    rows.append(json.loads(line))
                                except json.JSONDecodeError:
                                    break
                        elapsed = max((r["end_offset_s"] for r in rows), default=0)
                        metrics = summarize(rows, definition["questions"], job["settings"]["rate"],
                            job["settings"]["seconds"], elapsed, len(rows), job.get("metrics", {}).get("peak_concurrency", 0), True, True)
                        status = "interrupted"
                        (directory / "result.json").write_text(packed({"metrics": metrics, "status": status}))
                    await asyncio.to_thread(jobs.update, job["id"], state="archiving", metrics=metrics)
                    await asyncio.to_thread(archive, history, job, definition, directory, metrics, status)
                    await asyncio.to_thread(jobs.update, job["id"], state=status, metrics=metrics, finished_at=time.time())
                    await asyncio.to_thread(jobs.release, job["id"])
                    # Complete immutable evidence is in S3; keep only the local summary.
                    (directory / "requests.jsonl").unlink(missing_ok=True)
                    print(packed({"run": job["id"], "status": status,
                                  "completed": metrics["completed"], "p50_ms": metrics["p50_ms"]}), flush=True)
                except Exception as error:
                    # Keep the spool and active job for recovery; never silently re-run inference.
                    await asyncio.to_thread(jobs.update, job["id"], state="archiving", error=type(error).__name__)
                    print(packed({"run": job["id"], "archive_error": type(error).__name__}), flush=True)
                    await asyncio.sleep(5)
                finally:
                    # Only the job's independent GPU lease ends. Other presenters retain theirs.
                    try:
                        await client.post(f"/api/{job['scenario']}/end", json={})
                    except httpx.HTTPError:
                        pass  # The durable 600s lease still expires if the API is unavailable.
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


if __name__ == "__main__":
    import uvloop
    uvloop.run(main())
