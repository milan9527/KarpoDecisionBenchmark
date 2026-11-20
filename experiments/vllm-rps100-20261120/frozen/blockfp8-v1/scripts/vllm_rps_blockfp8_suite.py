"""Small-batch replica sweep. Stops model children, never shuts down EC2."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import traceback

import boto3
from botocore.config import Config
import httpx

from vllm_rps_benchmark import dump

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"


def stop(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def wait_health(process, url, limit=1200):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited {process.returncode}: {url}")
        try:
            response = httpx.get(url + "/healthz", timeout=3, trust_env=False)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            time.sleep(2)
    raise TimeoutError(f"Server not ready: {url}")


def main(args):
    directory = EXP / "results/runs" / args.name
    directory.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((EXP / f"manifest-{args.revision}.json").read_text())
    for path, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    dump(directory / "manifest.json", manifest)
    dump(directory / "parameters.json", vars(args))
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_XET": "1"}
    python = ROOT / "venv-vllm034/bin/python"
    (directory / "environment.json").write_bytes(subprocess.check_output(
        [str(python), str(ROOT / "scripts/vllm_single_environment.py")], env=env))
    (directory / "gpu-hardware.txt").write_text(subprocess.check_output(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv"], text=True))
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2}))
    done = threading.Event()
    sync_lock = threading.Lock()
    signatures = {}

    def sync():
        with sync_lock:
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if signatures.get(str(path)) != signature:
                    s3.upload_file(str(path), BUCKET, f"results/{EXP.name}/" + str(path.relative_to(EXP / "results")))
                    signatures[str(path)] = signature

    def periodic():
        while not done.wait(15):
            try:
                sync()
            except Exception:
                print(traceback.format_exc(), flush=True)

    thread = threading.Thread(target=periodic, daemon=True)
    thread.start()
    status = {"status": "running", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "ec2_policy": "keep_running", "stages": []}
    dump(directory / "status.json", status)
    gpu_log = (directory / "gpu.csv").open("x")
    monitor = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
         "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        for batch in args.batches:
            config_dir = directory / f"b{batch}"
            config_dir.mkdir()
            servers, logs = [], []
            try:
                urls = []
                for replica in range(args.gpus):
                    out = config_dir / f"server-{replica}"
                    out.mkdir()
                    port = 18500 + replica
                    url = f"http://127.0.0.1:{port}"
                    urls.append(url)
                    command = [
                        str(python), str(ROOT / "scripts/vllm_rps_blockfp8_server.py"),
                        "--checkpoint", str(ROOT / "models/karpo-4b"), "--output", str(out),
                        "--max-seqs", str(batch), "--replica-id", str(replica), "--port", str(port),
                        "--admission-limit", "64",
                    ]
                    dump(out / "command.json", command)
                    log = (out / "server.log").open("x")
                    logs.append(log)
                    servers.append(subprocess.Popen(
                        command, env={**env, "CUDA_VISIBLE_DEVICES": str(replica)},
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
                for replica, (server, url) in enumerate(zip(servers, urls)):
                    dump(config_dir / f"server-{replica}/health.json", wait_health(server, url))
                log = (config_dir / "proxy.log").open("x")
                logs.append(log)
                proxy = subprocess.Popen(
                    [str(python), str(ROOT / "scripts/vllm_rps_proxy.py"), "--urls", *urls],
                    env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                servers.append(proxy)
                wait_health(proxy, "http://127.0.0.1:18450")
                for replicas in args.replicas:
                    response = httpx.post("http://127.0.0.1:18450/configure",
                                          json={"replicas": replicas}, timeout=10, trust_env=False)
                    response.raise_for_status()
                    for scenario in ("risk", "sop"):
                        stages = [("quality", None)] if not args.skip_quality else []
                        if not args.only_target:
                            stages.append(("low-load", (20 if scenario == "risk" else 8) * replicas))
                        stages.append(("rps100", 100))
                        for name, rate in stages:
                            target = config_dir / f"r{replicas}" / scenario / name
                            entry = {"batch": batch, "replicas": replicas, "scenario": scenario, "stage": name}
                            status["current"] = entry
                            dump(directory / "status.json", status)
                            command = [
                                str(python), str(ROOT / "scripts/vllm_rps_benchmark.py"),
                                "--scenario", scenario, "--output", str(target),
                            ]
                            if rate is None:
                                command += ["--mode", "quality", "--concurrency", str(min(32, batch * replicas))]
                            else:
                                command += ["--rps", str(rate), "--seconds", str(args.seconds)]
                            with (config_dir / "benchmark.log").open("a") as log_bench:
                                subprocess.run(command, env=env, stdout=log_bench, stderr=subprocess.STDOUT,
                                               timeout=max(600, args.seconds + 180), check=True)
                            result = json.loads((target / "summary.json").read_text())
                            entry = {**entry, "result": {k: v for k, v in result.items() if k != "backlog_samples"}}
                            status["stages"].append(entry)
                            dump(directory / "status.json", status)
                            print(json.dumps(entry), flush=True)
                            sync()
                            if rate is None and (result["failures"] or result["accuracy"] <= .9):
                                raise RuntimeError(f"Quality gate failed: {target}")
            finally:
                for server in reversed(servers):
                    stop(server)
                for log in logs:
                    log.close()
                sync()
        status["status"] = "complete"
    except Exception:
        status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        stop(monitor)
        gpu_log.close()
        status["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump(directory / "status.json", status)
        done.set()
        thread.join(timeout=35)
        sync()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--revision", default="blockfp8-v1")
    parser.add_argument("--gpus", type=int, default=2)
    parser.add_argument("--replicas", nargs="+", type=int, default=[1, 2])
    parser.add_argument("--batches", nargs="+", type=int, default=[1, 2, 4, 8])
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--skip-quality", action="store_true")
    parser.add_argument("--only-target", action="store_true")
    args = parser.parse_args()
    assert 1 <= min(args.replicas) <= max(args.replicas) <= args.gpus
    main(args)
