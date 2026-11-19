"""Run official Karpo serving paths and periodically persist evidence to private S3."""
import argparse
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import threading
import time
import traceback

import boto3
import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / "venv/bin/python")


class EvidenceSync:
    def __init__(self, bucket):
        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.seen = {}
        self.stopped = threading.Event()

    def once(self):
        for path in (ROOT / "results").rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if self.seen.get(str(path)) == signature:
                continue
            self.s3.upload_file(str(path), self.bucket, str(path.relative_to(ROOT)))
            self.seen[str(path)] = signature
        bootstrap = pathlib.Path("/var/log/karpo-bootstrap.log")
        if bootstrap.exists():
            self.s3.upload_file(str(bootstrap), self.bucket, "results/bootstrap.log")

    def watch(self):
        while not self.stopped.is_set():
            try:
                self.once()
            except Exception as exc:
                print("Evidence sync error:", repr(exc), flush=True)
            self.stopped.wait(15)


def halt(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main(args):
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    sync = EvidenceSync(args.bucket)
    thread = threading.Thread(target=sync.watch, daemon=True)
    thread.start()
    gpu_log = (results / "gpu.csv").open("w")
    gpu = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,name,memory.used,utilization.gpu,power.draw",
                            "--format=csv", "-l", "1"], stdout=gpu_log, stderr=subprocess.STDOUT,
                           start_new_session=True)
    configs = [
        ("31b-bf16", []),
        ("31b-bf16-triton-graphs", ["--triton-attention", "--graphs"]),
        ("31b-fp8-triton-graphs", ["--fp8", "--triton-attention", "--graphs"]),
    ]
    if args.only:
        configs = [item for item in configs if item[0] == args.only]
    outcomes = []
    server = None
    try:
        for name, flags in configs:
            print("START CONFIG", name, flush=True)
            server_log = (results / (name + "-server.log")).open("w")
            command = [PYTHON, "-m", "torch.distributed.run", "--standalone", "--nproc_per_node", "1",
                       "-m", "karpo_decision.serve.server", "--checkpoint", str(ROOT / "models/karpo-31b"),
                       "--name", name, "--host", "127.0.0.1", "--port", "18440",
                       "--max-length", "32768", *flags]
            (results / (name + "-command.json")).write_text(json.dumps(command, indent=2) + "\n")
            env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
                   "PYTHONUNBUFFERED": "1"}
            server = subprocess.Popen(command, stdout=server_log, stderr=subprocess.STDOUT, env=env,
                                      start_new_session=True)
            start = time.monotonic()
            ready = False
            while time.monotonic() - start < 900:
                if server.poll() is not None:
                    break
                try:
                    r = httpx.get("http://127.0.0.1:18440/healthz", timeout=2, trust_env=False)
                    if r.status_code == 200:
                        ready = True
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(3)
            if not ready:
                outcomes.append({"name": name, "status": "startup_failed", "returncode": server.poll()})
                halt(server)
                server_log.close()
                sync.once()
                continue
            print("READY", name, "startup_seconds", round(time.monotonic() - start, 2), flush=True)
            bench_log = (results / (name + "-benchmark.log")).open("w")
            completed = subprocess.run([PYTHON, str(ROOT / "scripts/benchmark.py"), "--name", name,
                                        "--flags", " ".join(flags) or "BF16", "--phase", args.phase],
                                       stdout=bench_log, stderr=subprocess.STDOUT, env=env)
            bench_log.close()
            outcomes.append({"name": name, "status": "complete" if completed.returncode == 0 else "benchmark_failed",
                             "returncode": completed.returncode})
            print("FINISHED", name, completed.returncode, flush=True)
            halt(server)
            server = None
            server_log.close()
            (results / "suite-progress.json").write_text(json.dumps(outcomes, indent=2) + "\n")
            sync.once()
    except Exception:
        (results / "suite-error.txt").write_text(traceback.format_exc())
        raise
    finally:
        halt(server)
        halt(gpu)
        gpu_log.close()
        (results / "suite-outcomes.json").write_text(json.dumps({
            "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "configs": outcomes,
        }, indent=2) + "\n")
        sync.stopped.set()
        thread.join(timeout=20)
        sync.once()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--only", choices=["31b-bf16", "31b-bf16-triton-graphs", "31b-fp8-triton-graphs"])
    p.add_argument("--phase", choices=["all", "smoke", "quality", "performance"], default="all")
    main(p.parse_args())
