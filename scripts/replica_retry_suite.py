"""Frozen one-versus-two replica experiment, after the full family suite."""
import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback

import boto3
from botocore.config import Config
import httpx

from benchmark import Runner, summarize, validate
from concise_suite import halt

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/replicas-retry-20261120"
FAMILY = ROOT / "experiments/family-20261120"
QWEN = ROOT / "experiments/qwen-retry-20261120"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load(path):
    return json.loads(path.read_text())


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def select():
    status = load(FAMILY / "results/suite-status.json")
    assert status["status"] == "partial"
    assert load(QWEN / "results/suite-status.json")["status"] == "complete"
    manifest = load(FAMILY / "manifest.json")
    fixture = {r["id"]: r for r in load(FAMILY / "requests.json")}
    observed = {}
    for config in manifest["configs"]:
        if config["precision"] != "fp8":
            continue
        source = QWEN if config["model"] in ["9B", "4B"] else FAMILY
        rows = [json.loads(line) for line in
                (source / "results" / config["id"] / "requests.jsonl").read_text().splitlines()]
        quality = [r for r in rows if r["stage"].startswith("quality-")]
        assert len(quality) == 104 and {r["request_id"] for r in quality} == set(fixture)
        for row in quality:
            item = fixture[row["request_id"]]
            assert row["request_sha256"] == item["request_sha256"]
            if row["valid"]:
                assert validate(row["response"], item) == row["correct"]
        observed[config["id"]] = quality
    baseline = {r["request_id"]: r for r in observed["31b-fp8"]}
    candidates = []
    for config in manifest["configs"]:
        if config["id"] not in observed:
            continue
        quality = observed[config["id"]]
        new_errors = sum(baseline[r["request_id"]]["correct"] and not r["correct"] for r in quality)
        summary = summarize(quality, 0)
        candidates.append({"config": config, "valid": summary["valid"], "correct": summary["correct"],
                           "new_errors_vs_31b": new_errors, "p50_ms": summary["p50_ms"]})
    selected = min((c for c in candidates if c["valid"] == 104 and c["new_errors_vs_31b"] == 0),
                   key=lambda c: c["p50_ms"])
    return selected["config"], {"selected_utc": utc(), "selected": selected, "candidates": candidates}


async def measure(target, ranks, manifest):
    items = load(FAMILY / "requests.json")
    quality = [r for r in items if r["group"] == "synthetic"]
    boundary = [r for r in items if r["group"] == "boundary"]
    performance = [r for r in quality if r["order"] == "original"][::5]
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18440", timeout=60, trust_env=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=40),
    ) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        assert health.json()["ranks"] == ranks
        write(target / "health.json", health.json())
        runner = Runner(client, target)
        try:
            await runner.serial("warmup-excluded", performance)
            await runner.serial("quality-existing", quality)
            await runner.serial("quality-boundary", boundary)
            for count in manifest["concurrency"]:
                result = await runner.closed_loop(f"concurrency-{count}", performance, count,
                                                  manifest["concurrency_requests"])
                if result["failures"]:
                    raise RuntimeError("Invalid responses; stop increasing concurrency")
            write(target / "complete.json", {"status": "complete", "observations": runner.counter})
        finally:
            runner.raw.close()


def main(bucket):
    manifest = load(EXP / "manifest.json")
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    result = EXP / "results"
    result.mkdir(exist_ok=True)
    status = {"started_utc": utc(), "status": "selecting", "configs": []}
    write(result / "suite-status.json", status)
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}))
    ending, lock = threading.Event(), threading.Lock()
    signatures = {}

    def sync():
        with lock:
            for path in result.rglob("*"):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                st = path.stat()
                signature = (st.st_size, st.st_mtime_ns)
                if signatures.get(str(path)) != signature:
                    s3.upload_file(str(path), bucket, "results/replicas-retry-20261120/" + str(path.relative_to(result)))
                    signatures[str(path)] = signature

    def periodic():
        while not ending.is_set():
            try:
                sync()
            except Exception as exc:
                print("Sync failed", str(exc), flush=True)
            ending.wait(15)

    thread = threading.Thread(target=periodic, daemon=True)
    thread.start()
    server = gpu = None
    gpu_log = None
    try:
        config, selection = select()
        write(result / "selection.json", selection)
        gpu_log = (result / "gpu.csv").open("x")
        gpu = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,index,name,memory.used,utilization.gpu,power.draw",
             "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT, start_new_session=True)
        for ranks in manifest["replicas"]:
            target = result / f"replicas-{ranks}"
            target.mkdir(exist_ok=False)
            entry = {"id": f"replicas-{ranks}", "ranks": ranks, "started_utc": utc(), "status": "starting"}
            status["configs"].append(entry)
            status.update(status="starting", active=entry["id"])
            write(result / "suite-status.json", status)
            command = [
                str(ROOT / "venv/bin/python"), "-m", "torch.distributed.run", "--standalone",
                "--nproc_per_node", str(ranks), "-m", "karpo_decision.serve.server",
                "--checkpoint", str(ROOT / "models" / ("karpo-" + config["model"].lower())),
                "--name", entry["id"], "--host", "127.0.0.1", "--port", "18440",
                "--max-length", str(manifest["max_length"]), *config["flags"],
            ]
            write(target / "server-command.json", command)
            try:
                with (target / "server.log").open("x") as output:
                    start = time.monotonic()
                    server = subprocess.Popen(
                        command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                        env={**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
                             "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_XET": "1"})
                    while time.monotonic() - start < 1500:
                        if server.poll() is not None:
                            raise RuntimeError(f"Server exited: {server.returncode}")
                        try:
                            health = httpx.get("http://127.0.0.1:18440/healthz", timeout=2, trust_env=False)
                            if health.status_code == 200 and health.json()["ranks"] == ranks:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(3)
                    else:
                        raise RuntimeError("Server startup timed out")
                    entry.update(status="benchmarking", startup_seconds=time.monotonic() - start)
                    status["status"] = "benchmarking"
                    write(result / "suite-status.json", status)
                    asyncio.run(measure(target, ranks, manifest))
                    entry["status"] = "complete"
            except Exception:
                entry.update(status="failed", error=traceback.format_exc())
                write(target / "error.json", entry)
            finally:
                halt(server)
                server = None
                entry["ended_utc"] = utc()
                write(result / "suite-status.json", status)
                sync()
        status["status"] = "complete" if all(c["status"] == "complete" for c in status["configs"]) else "partial"
    except Exception:
        status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        halt(server)
        halt(gpu)
        if gpu_log:
            gpu_log.close()
        status["ended_utc"] = utc()
        write(result / "suite-status.json", status)
        apps = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"],
            text=True).strip()
        write(result / "postflight.json", {"observed_utc": utc(), "gpu_compute_processes": apps,
                                         "suite_status": status["status"]})
        ending.set()
        thread.join(timeout=35)
        sync()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    main(parser.parse_args().bucket)
