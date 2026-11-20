"""Download pinned checkpoints, serve configurations serially, preserve every result."""
import argparse
import concurrent.futures
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
from huggingface_hub import snapshot_download
import httpx
import torch
import transformers

import karpo_decision
from concise_suite import halt

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/qwen-retry-20261120"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def main(bucket):
    manifest = json.loads((EXP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    result = EXP / "results"
    result.mkdir(exist_ok=True)
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}))
    ending, sync_lock = threading.Event(), threading.Lock()
    signatures = {}

    def sync():
        with sync_lock:
            for path in result.rglob("*"):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                st = path.stat()
                signature = (st.st_size, st.st_mtime_ns)
                if signatures.get(str(path)) != signature:
                    s3.upload_file(str(path), bucket, "results/qwen-retry-20261120/" + str(path.relative_to(result)))
                    signatures[str(path)] = signature

    def periodic():
        while not ending.is_set():
            try:
                sync()
            except Exception as exc:
                print("Sync failed:", str(exc), flush=True)
            ending.wait(15)

    thread = threading.Thread(target=periodic, daemon=True)
    thread.start()
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_XET": "1"}
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    python = str(ROOT / "venv/bin/python")
    provenance = json.loads(importlib.metadata.distribution("karpo-decision").read_text("direct_url.json"))
    assert provenance["vcs_info"]["commit_id"] == manifest["code_revision"], provenance
    package = Path(karpo_decision.__file__).parent
    environment = {
        "observed_utc": utc(), "torch": torch.__version__, "transformers": transformers.__version__,
        "cuda": torch.version.cuda, "code_revision": manifest["code_revision"],
        "gpu": subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], text=True).strip(),
        "installed_source": provenance,
        "runtime_source_sha256": {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted(package.rglob("*.py"))},
    }
    from karpo_decision.families.qwen.packed import _kernels
    _kernels()
    environment["qwen_dependencies"] = {name: importlib.metadata.version(name) for name in
                                        ["flash-linear-attention", "fla-core"]}
    assert environment["qwen_dependencies"] == {"flash-linear-attention": "0.5.2", "fla-core": "0.5.2"}
    assert environment["torch"] == "2.14.1+cu130"
    assert environment["transformers"] == "5.21.0"
    write(result / "environment.json", environment)
    gpu_log = (result / "gpu.csv").open("x")
    gpu = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=timestamp,name,memory.used,utilization.gpu,power.draw",
         "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT, start_new_session=True)
    status = {"started_utc": utc(), "status": "downloading", "configs": []}
    write(result / "suite-status.json", status)
    server = None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def download(model):
        directory = ROOT / "models" / ("karpo-" + model["size"].lower())
        start = time.monotonic()
        details = {"model": model, "started_utc": utc(), "status": "downloading"}
        write(result / ("download-" + model["size"] + ".json"), details)
        snapshot_download(model["id"], revision=model["revision"], local_dir=str(directory),
                          ignore_patterns=["assets/*", "*.png"], max_workers=8)
        weights = sum(p.stat().st_size for p in directory.glob("*.safetensors"))
        assert weights == model["weights_bytes"], (model["size"], weights)
        assert directory.resolve().stat().st_dev == Path("/").stat().st_dev, "Weights must remain on EBS"
        details.update(status="complete", seconds=time.monotonic() - start,
                       ended_utc=utc(), weights_bytes=weights, directory=str(directory.resolve()))
        write(result / ("download-" + model["size"] + ".json"), details)
        return directory

    # Download one model at a time, overlapping transfer with earlier models' GPU work.
    downloads = {m["size"]: pool.submit(download, m) for m in manifest["models"]}
    try:
        for config in manifest["configs"]:
            directory = downloads[config["model"]].result()
            target = result / config["id"]
            target.mkdir(exist_ok=False)
            command = [
                python, "-m", "torch.distributed.run", "--standalone", "--nproc_per_node", "1",
                "-m", "karpo_decision.serve.server", "--checkpoint", str(directory),
                "--name", config["id"], "--host", "127.0.0.1", "--port", "18440",
                "--max-length", str(manifest["max_length"]), *config["flags"],
            ]
            write(target / "server-command.json", command)
            entry = {"id": config["id"], "started_utc": utc(), "status": "starting"}
            status["configs"].append(entry)
            status.update(status="starting", active=config["id"])
            write(result / "suite-status.json", status)
            print("START", config["id"], flush=True)
            try:
                with (target / "server.log").open("x") as output:
                    start = time.monotonic()
                    server = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                              env=env, start_new_session=True)
                    ready = False
                    while time.monotonic() - start < 1500:
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
                        raise RuntimeError(f"Server startup failed or timed out: exit={server.poll()}")
                    entry.update(status="benchmarking", startup_seconds=time.monotonic() - start)
                    status["status"] = "benchmarking"
                    write(result / "suite-status.json", status)
                    with (target / "benchmark.log").open("x") as log:
                        process = subprocess.run(
                            [python, str(ROOT / "scripts/qwen_retry_benchmark.py"), "--config", config["id"]],
                            stdout=log, stderr=subprocess.STDOUT, env=env, timeout=1800)
                    if process.returncode:
                        raise RuntimeError(f"Benchmark exit={process.returncode}")
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
        status["status"] = "complete" if all(e["status"] == "complete" for e in status["configs"]) else "partial"
    except Exception:
        status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        halt(server)
        halt(gpu)
        gpu_log.close()
        pool.shutdown(wait=True)
        status["ended_utc"] = utc()
        write(result / "suite-status.json", status)
        apps = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"],
            text=True).strip()
        write(result / "postflight.json", {
            "observed_utc": utc(), "suite_status": status["status"], "gpu_compute_processes": apps,
            "models": [{"size": m["size"], "directory": str((ROOT / "models" / ("karpo-" + m["size"].lower())).resolve()),
                        "weights_bytes": sum(p.stat().st_size for p in
                                             (ROOT / "models" / ("karpo-" + m["size"].lower())).glob("*.safetensors"))}
                       for m in manifest["models"]],
        })
        ending.set()
        thread.join(timeout=35)
        sync()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    main(parser.parse_args().bucket)
