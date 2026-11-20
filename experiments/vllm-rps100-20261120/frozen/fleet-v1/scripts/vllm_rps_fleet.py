"""Keep original-input replicas on private hosts; benchmark the complete proxy path."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import traceback

import boto3
from botocore.config import Config
import httpx

from vllm_rps_benchmark import dump
from vllm_rps_suite import stop, wait_health

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"
PYTHON = ROOT / "venv-vllm034/bin/python"


class Evidence:
    def __init__(self, args):
        self.directory = EXP / "results/runs" / args.name
        self.directory.mkdir(parents=True, exist_ok=False)
        manifest = json.loads((EXP / f"manifest-{args.revision}.json").read_text())
        for path, expected in manifest["files"].items():
            assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
        dump(self.directory / "manifest.json", manifest)
        dump(self.directory / "parameters.json", vars(args))
        self.status = {"status": "running", "ec2_policy": "keep_running", "stages": [],
                       "started_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        self.save()
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.signatures = {}
        self.s3 = None if args.roleless else boto3.client(
            "s3", region_name="us-east-1",
            config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2}))
        self.thread = threading.Thread(target=self.periodic, daemon=True)
        self.thread.start()

    def save(self):
        dump(self.directory / "status.json", self.status)

    def sync(self):
        if self.s3 is None:
            return
        with self.lock:
            for path in sorted(self.directory.rglob("*")):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if self.signatures.get(str(path)) == signature:
                    continue
                self.s3.upload_file(str(path), "karpo-benchmark-20261119-115003-632930644527",
                                    f"results/{EXP.name}/" + str(path.relative_to(EXP / "results")))
                self.signatures[str(path)] = signature

    def periodic(self):
        while not self.finished.wait(20):
            try:
                self.sync()
            except Exception:
                traceback.print_exc()

    def finish(self, error=None):
        self.status.update(status="failed" if error else "complete",
                           ended_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        if error:
            self.status["error"] = error
        self.save()
        self.finished.set()
        self.thread.join(timeout=35)
        self.sync()


def node(args, evidence, processes, logs):
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1"}
    (evidence.directory / "environment.json").write_bytes(subprocess.check_output(
        [str(PYTHON), str(ROOT / "scripts/vllm_single_environment.py")], env=env))
    (evidence.directory / "gpu-hardware.txt").write_text(subprocess.check_output(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv"], text=True))
    urls = []
    for gpu, replica in enumerate(args.replica_ids):
        directory = evidence.directory / f"b{args.batch}/server-{replica}"
        directory.mkdir(parents=True)
        port = 18500 + gpu
        command = [str(PYTHON), str(ROOT / "scripts/vllm_rps_server.py"),
                   "--checkpoint", str(ROOT / "models/karpo-4b"), "--output", str(directory),
                   "--max-seqs", str(args.batch), "--replica-id", str(replica),
                   "--port", str(port), "--host", "0.0.0.0", "--admission-limit", "64"]
        dump(directory / "command.json", command)
        log = (directory / "server.log").open("x")
        logs.append(log)
        processes.append(subprocess.Popen(
            command, env={**env, "CUDA_VISIBLE_DEVICES": str(gpu)},
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        urls.append(f"http://127.0.0.1:{port}")
    for replica, process, url in zip(args.replica_ids, processes, urls, strict=True):
        health = wait_health(process, url)
        assert health["replica_id"] == replica
        dump(evidence.directory / f"b{args.batch}/server-{replica}/health.json", health)
    gpu_log = (evidence.directory / "gpu.csv").open("x")
    logs.append(gpu_log)
    processes.append(subprocess.Popen(
        ["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
         "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT, start_new_session=True))
    evidence.status["ready"] = True
    evidence.save()
    evidence.sync()
    requested_stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: requested_stop.set())
    signal.signal(signal.SIGINT, lambda *_: requested_stop.set())
    while not requested_stop.wait(5):
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"Replica process exited: {process.returncode}")


def driver(args, evidence, processes, logs):
    for replica, url in enumerate(args.urls):
        response = httpx.get(url + "/healthz", timeout=10, trust_env=False)
        response.raise_for_status()
        assert response.json()["replica_id"] == replica, (url, response.json())
    log = (evidence.directory / "proxy.log").open("x")
    logs.append(log)
    proxy = subprocess.Popen(
        [str(PYTHON), str(ROOT / "scripts/vllm_rps_proxy.py"), "--urls", *args.urls],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    processes.append(proxy)
    dump(evidence.directory / "health.json", wait_health(proxy, "http://127.0.0.1:18450"))
    replicas = len(args.urls)
    for scenario in args.scenarios:
        stages = [("quality", None), ("rps100", args.seconds)]
        if args.confirm:
            stages += [("confirm", 120), ("repeat", 120)]
        for stage, seconds in stages:
            target = evidence.directory / f"b{args.batch}/r{replicas}/{scenario}/{stage}"
            entry = {"batch": args.batch, "replicas": replicas, "scenario": scenario, "stage": stage}
            evidence.status["current"] = entry
            evidence.save()
            command = [str(PYTHON), str(ROOT / "scripts/vllm_rps_benchmark.py"),
                       "--scenario", scenario, "--output", str(target)]
            if seconds is None:
                command += ["--mode", "quality", "--concurrency", str(replicas)]
            else:
                command += ["--rps", "100", "--seconds", str(seconds)]
            with (evidence.directory / "benchmark.log").open("a") as bench_log:
                subprocess.run(command, check=True, timeout=max(600, (seconds or 0) + 180),
                               stdout=bench_log, stderr=subprocess.STDOUT)
            result = json.loads((target / "summary.json").read_text())
            evidence.status["stages"].append({**entry, "result": result})
            evidence.save()
            evidence.sync()
            if seconds is None and (result["failures"] or result["accuracy"] <= .9):
                raise RuntimeError(f"Quality gate failed: {scenario}")
            # A rejected probe cannot advance to confirmation, but the other
            # scenario must still be measured independently.
            if seconds is not None and not result["passes_probe"]:
                break


def main(args):
    evidence = Evidence(args)
    processes, logs = [], []
    error = None
    try:
        (node if args.mode == "node" else driver)(args, evidence, processes, logs)
    except Exception:
        error = traceback.format_exc()
        raise
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        evidence.finish(error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["node", "driver"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--revision", default="fleet-v1")
    parser.add_argument("--batch", type=int, choices=[1, 2, 4, 8], default=4)
    parser.add_argument("--replica-ids", type=int, nargs="+")
    parser.add_argument("--urls", nargs="+")
    parser.add_argument("--scenarios", nargs="+", choices=["risk", "sop"], default=["risk", "sop"])
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--roleless", action="store_true")
    args = parser.parse_args()
    assert args.replica_ids if args.mode == "node" else args.urls
    main(args)
