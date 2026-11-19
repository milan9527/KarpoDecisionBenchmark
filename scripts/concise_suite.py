"""Start one official FP8 server, run the frozen experiment, sync, then exit."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import traceback

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/concise-fp8-20261119"


def halt(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main(bucket):
    result = EXP / "results"
    result.mkdir(exist_ok=True)
    s3 = boto3.client("s3", region_name="us-east-1")
    ending = threading.Event()
    signatures = {}
    sync_lock = threading.Lock()

    def sync():
        with sync_lock:
            for path in result.rglob("*"):
                if not path.is_file():
                    continue
                st = path.stat()
                signature = (st.st_size, st.st_mtime_ns)
                if signatures.get(str(path)) != signature:
                    s3.upload_file(str(path), bucket,
                                   "results/concise-fp8-20261119/" + str(path.relative_to(result)))
                    signatures[str(path)] = signature

    def periodic_sync():
        while not ending.is_set():
            try:
                sync()
            except Exception as exc:
                print("Sync failed:", str(exc), flush=True)
            ending.wait(15)

    thread = threading.Thread(target=periodic_sync, daemon=True)
    thread.start()
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1"}
    python = str(ROOT / "venv/bin/python")
    gpu_log = (result / "gpu.csv").open("x")
    gpu = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=timestamp,name,memory.used,utilization.gpu,power.draw",
         "--format=csv", "-l", "1"], stdout=gpu_log, stderr=subprocess.STDOUT, start_new_session=True)
    server = None
    status = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "starting"}
    try:
        command = [python, "-m", "torch.distributed.run", "--standalone", "--nproc_per_node", "1",
                   "-m", "karpo_decision.serve.server", "--checkpoint", str(ROOT / "models/karpo-31b"),
                   "--name", "concise-fp8", "--host", "127.0.0.1", "--port", "18440",
                   "--max-length", "32768", "--fp8", "--triton-attention", "--graphs"]
        (result / "server-command.json").write_text(json.dumps(command, indent=2) + "\n")
        with (result / "server.log").open("x") as log:
            start = time.monotonic()
            server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                      start_new_session=True)
            ready = False
            while time.monotonic() - start < 1200:
                if server.poll() is not None:
                    break
                try:
                    response = httpx.get("http://127.0.0.1:18440/healthz", timeout=2, trust_env=False)
                    if response.status_code == 200:
                        ready = True
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(3)
            if not ready:
                raise RuntimeError(f"Server startup failed or timed out; code={server.poll()}")
            status.update(status="benchmarking", startup_seconds=time.monotonic() - start)
            (result / "suite-status.json").write_text(json.dumps(status, indent=2) + "\n")
            print("SERVER READY", status["startup_seconds"], flush=True)
            with (result / "benchmark.log").open("x") as output:
                completed = subprocess.run([python, str(ROOT / "scripts/concise_benchmark.py")],
                                           env=env, stdout=output, stderr=subprocess.STDOUT)
            if completed.returncode:
                raise RuntimeError(f"Benchmark failed with exit {completed.returncode}")
            status["status"] = "complete"
    except Exception:
        status["status"] = "failed"
        (result / "error.txt").write_text(traceback.format_exc())
        raise
    finally:
        halt(server)
        halt(gpu)
        gpu_log.close()
        status["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (result / "suite-status.json").write_text(json.dumps(status, indent=2) + "\n")
        ending.set()
        thread.join(timeout=20)
        sync()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    main(parser.parse_args().bucket)
