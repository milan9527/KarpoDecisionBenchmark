"""Run frozen batch100 phases and periodically preserve results in the existing bucket."""
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
EXP = ROOT / "experiments/batch100-20261120"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main(bucket):
    import torch
    import transformers
    import karpo_decision
    result = EXP / "results"
    manifest = json.loads((EXP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    provenance = json.loads(importlib.metadata.distribution("karpo-decision").read_text("direct_url.json"))
    assert provenance["vcs_info"]["commit_id"] == manifest["code_revision"]
    package = Path(karpo_decision.__file__).parent
    model = ROOT / "models/karpo-4b"
    assert sum(p.stat().st_size for p in model.glob("*.safetensors")) == 9081227392
    write(result / "environment.json", {
        "observed_utc": utc(), "torch": torch.__version__, "transformers": transformers.__version__,
        "cuda": torch.version.cuda, "code_source": provenance,
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
    done = threading.Event()
    sync_lock = threading.Lock()
    signatures = {}

    def sync():
        with sync_lock:
            for p in sorted(result.rglob("*")):
                if not p.is_file() or p.suffix == ".tmp":
                    continue
                st = p.stat()
                signature = (st.st_size, st.st_mtime_ns)
                if signatures.get(str(p)) == signature:
                    continue
                s3.upload_file(str(p), bucket, "results/batch100-20261120/" + str(p.relative_to(result)))
                signatures[str(p)] = signature

    def periodic():
        while not done.wait(20):
            try:
                sync()
            except Exception:
                print(traceback.format_exc(), flush=True)
    thread = threading.Thread(target=periodic, daemon=True)
    thread.start()
    status = {"started_utc": utc(), "status": "starting", "phases": []}
    write(result / "suite-status.json", status)
    gpu_log = (result / "gpu.csv").open("x")
    gpu = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
                            "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT,
                           start_new_session=True)
    server = None
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_XET": "1"}
    python = str(ROOT / "venv/bin/python")
    try:
        for phase in ["bf16", "fp8"]:
            directory = result / phase
            directory.mkdir(exist_ok=True)
            entry = {"phase": phase, "started_utc": utc(), "status": "starting"}
            status["phases"].append(entry)
            command = [python, str(ROOT / "scripts/batch100_server.py"), "--checkpoint", str(model),
                       "--experiment", str(EXP), "--precision", phase, "--replicas", "1" if phase == "bf16" else "2"]
            if phase == "fp8":
                command.append("--graphs")
            write(directory / "server-command.json", command)
            write(result / "suite-status.json", status)
            with (directory / "server.log").open("x") as log:
                server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                          start_new_session=True)
                began = time.monotonic()
                while time.monotonic() - began < 2400:
                    if server.poll() is not None:
                        raise RuntimeError(f"{phase} server exited {server.returncode}")
                    try:
                        r = httpx.get("http://127.0.0.1:18450/healthz", timeout=2, trust_env=False)
                        if r.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(3)
                else:
                    raise TimeoutError(f"{phase} startup timeout")
                entry.update(status="benchmarking", startup_seconds=time.monotonic() - began)
                status["status"] = "benchmarking"
                write(result / "suite-status.json", status)
                sync()
                with (directory / "benchmark.log").open("x") as bench:
                    subprocess.run([python, str(ROOT / "scripts/batch100_benchmark.py"), "--phase", phase],
                                   stdout=bench, stderr=subprocess.STDOUT, env=env, check=True, timeout=5400)
            halt(server)
            server = None
            entry.update(status="complete", ended_utc=utc())
            write(result / "suite-status.json", status)
            sync()
        status["status"] = "complete"
    except Exception:
        status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        halt(server)
        halt(gpu)
        gpu_log.close()
        status["ended_utc"] = utc()
        write(result / "suite-status.json", status)
        write(result / "postflight.json", {
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
    main(parser.parse_args().bucket)
