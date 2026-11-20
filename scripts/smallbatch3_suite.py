"""Run one backend per host, preserve evidence, and bound all subprocess lifetimes."""
import argparse
import datetime as dt
import hashlib
import importlib.metadata
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

from batch100_benchmark import write
from concise_suite import halt

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/smallbatch3-20261120"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def await_health(url, processes, seconds=1800):
    begin = time.monotonic()
    while time.monotonic() - begin < seconds:
        if any(p.poll() is not None for p in processes):
            raise RuntimeError("A server process exited during startup")
        try:
            response = httpx.get(url, timeout=3, trust_env=False)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise TimeoutError(f"Startup timeout: {url}")


def main(args):
    import torch
    import transformers
    import karpo_decision
    manifest = json.loads((EXP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    provenance = json.loads(importlib.metadata.distribution("karpo-decision").read_text("direct_url.json"))
    assert provenance["vcs_info"]["commit_id"] == manifest["code_revision"]
    model = ROOT / "models/karpo-4b"
    assert sum(p.stat().st_size for p in model.glob("*.safetensors")) == 9081227392
    assert torch.cuda.device_count() == (2 if args.role == "primary" else 1)
    result = EXP / "results"
    host = result / args.role
    host.mkdir(parents=True, exist_ok=True)
    package = Path(karpo_decision.__file__).parent
    write(host / "environment.json", {
        "observed_utc": utc(), "role": args.role, "torch": torch.__version__,
        "transformers": transformers.__version__, "cuda": torch.version.cuda, "code_source": provenance,
        "dependencies": {x: importlib.metadata.version(x) for x in ["flash-linear-attention", "fla-core"]},
        "gpu": subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                        "--format=csv,noheader"], text=True),
        "runtime_source_sha256": {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in package.rglob("*.py")},
        "model_metadata_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in model.glob("*.json")},
    })
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2}))
    done, lock, signatures = threading.Event(), threading.Lock(), {}

    def sync():
        with lock:
            roots = [host] + ([result / "measurements"] if args.role == "primary" else [])
            for root in roots:
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.suffix == ".tmp":
                        continue
                    stat = path.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    if signatures.get(str(path)) == signature:
                        continue
                    s3.upload_file(str(path), args.bucket,
                                   f"results/{EXP.name}/" + str(path.relative_to(result)))
                    signatures[str(path)] = signature

    def periodic():
        while not done.wait(20):
            try:
                sync()
            except Exception:
                print(traceback.format_exc(), flush=True)

    thread = threading.Thread(target=periodic, daemon=True)
    thread.start()
    status = {"status": "starting", "role": args.role, "started_utc": utc()}
    write(host / "suite-status.json", status)
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_XET": "1"}
    python = str(ROOT / "venv/bin/python")
    server = proxy = gpu = None
    logs = []
    try:
        gpu_log = (host / "gpu.csv").open("x")
        logs.append(gpu_log)
        gpu = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
                                "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT,
                               start_new_session=True)
        command = [python, str(ROOT / "scripts/smallbatch_server.py"), "--checkpoint", str(model),
                   "--experiment", str(EXP), "--replicas", "2" if args.role == "primary" else "1", "--host", "127.0.0.1" if args.role == "primary" else "0.0.0.0"]
        write(host / "server-command.json", command)
        log = (host / "server.log").open("x")
        logs.append(log)
        server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        health = await_health("http://127.0.0.1:18450/healthz", [server])
        write(host / "health.json", health)
        status.update(status="ready", ready_utc=utc())
        write(host / "suite-status.json", status)
        sync()
        if args.role == "secondary":
            # The coordinator signals completion using the existing private result bucket.
            started = time.monotonic()
            while time.monotonic() - started < 3600:
                if server.poll() is not None:
                    raise RuntimeError("Secondary server exited")
                try:
                    obj = s3.get_object(Bucket=args.bucket, Key=f"results/{EXP.name}/primary/suite-status.json")
                    primary = json.loads(obj["Body"].read())
                    if primary["status"] in ("complete", "failed"):
                        status.update(status="complete", coordinator_status=primary["status"])
                        break
                except s3.exceptions.NoSuchKey:
                    pass
                time.sleep(10)
            else:
                raise TimeoutError("Coordinator did not finish within one hour")
        else:
            assert args.secondary_url
            await_health(args.secondary_url + "/healthz", [server])
            command = [python, str(ROOT / "scripts/smallbatch3_proxy.py"), "--backends",
                       "http://127.0.0.1:18450", args.secondary_url]
            write(host / "proxy-command.json", command)
            log = (host / "proxy.log").open("x")
            logs.append(log)
            proxy = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                     start_new_session=True)
            write(host / "proxy-health.json", await_health("http://127.0.0.1:18451/healthz", [server, proxy]))
            status.update(status="benchmarking", benchmark_started_utc=utc())
            write(host / "suite-status.json", status)
            sync()
            with (host / "benchmark.log").open("x") as log:
                subprocess.run([python, str(ROOT / "scripts/smallbatch3_benchmark.py")],
                               stdout=log, stderr=subprocess.STDOUT, env=env, timeout=3600, check=True)
            status["status"] = "complete"
    except Exception:
        status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        halt(proxy)
        halt(server)
        halt(gpu)
        for log in logs:
            log.close()
        status["ended_utc"] = utc()
        write(host / "suite-status.json", status)
        write(host / "postflight.json", {
            "observed_utc": utc(),
            "gpu_compute_processes": subprocess.check_output(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"], text=True),
            "model_weights_bytes": sum(p.stat().st_size for p in model.glob("*.safetensors")),
        })
        done.set()
        thread.join(timeout=35)
        sync()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--role", choices=["primary", "secondary"], required=True)
    parser.add_argument("--secondary-url")
    main(parser.parse_args())
