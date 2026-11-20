"""Sequential single-GPU comparison with bounded children and periodic backups."""

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

from batch100_benchmark import write

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-single-20261120"
RESULT = EXP / "results"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stop(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def wait_health(process, limit=1200):
    begin = time.monotonic()
    while time.monotonic() - begin < limit:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with status {process.returncode}")
        try:
            response = httpx.get("http://127.0.0.1:18450/healthz", timeout=2, trust_env=False)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise TimeoutError("Model startup exceeded 20 minutes")


def main():
    RESULT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((EXP / "manifest.json").read_text())
    for path, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    assert not (RESULT / "suite-status.json").exists(), "Do not overwrite an earlier run"
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 2}))
    done = threading.Event()
    lock = threading.Lock()
    signatures = {}

    def sync():
        with lock:
            for path in sorted(RESULT.rglob("*")):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if signatures.get(str(path)) == signature:
                    continue
                s3.upload_file(str(path), BUCKET,
                               f"results/{EXP.name}/" + str(path.relative_to(RESULT)))
                signatures[str(path)] = signature

    def periodic():
        while not done.wait(20):
            try:
                sync()
            except Exception:
                print(traceback.format_exc(), flush=True)

    thread = threading.Thread(target=periodic, daemon=True)
    thread.start()
    status = {"status": "starting", "started_utc": utc(), "stages": []}
    write(RESULT / "suite-status.json", status)
    monitor = None
    gpu_log = None
    env = {
        **os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
        "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_XET": "1",
    }

    def stage(name, backend, batch=1, scenario=None, confirm=False):
        directory = RESULT / "servers" / name
        directory.mkdir(parents=True, exist_ok=False)
        if backend == "native":
            python = ROOT / "venv/bin/python"
            command = [
                str(python), str(ROOT / "scripts/smallbatch_server.py"),
                "--checkpoint", str(ROOT / "models/karpo-4b"), "--experiment", str(EXP),
                "--replicas", "1", "--max-batch", "1",
            ]
        else:
            python = ROOT / "venv-vllm034/bin/python"
            command = [
                str(python), str(ROOT / "scripts/vllm_karpo_server.py"),
                "--checkpoint", str(ROOT / "models/karpo-4b"),
                "--output", str(directory), "--max-seqs", str(batch),
            ]
        write(directory / "command.json", command)
        server = None
        entry = {"name": name, "backend": backend, "batch": batch, "started_utc": utc()}
        status.update(status="running", current=name)
        status["stages"].append(entry)
        write(RESULT / "suite-status.json", status)
        sync()
        try:
            with (directory / "server.log").open("x") as log:
                server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                          env=env, start_new_session=True)
                write(directory / "health.json", wait_health(server))
                bench_cmd = [
                    str(ROOT / "venv/bin/python"), str(ROOT / "scripts/vllm_single_benchmark.py"),
                    "--backend", backend, "--name", name,
                ]
                if scenario:
                    bench_cmd += ["--scenario", scenario]
                if confirm:
                    bench_cmd.append("--confirm")
                with (directory / "benchmark.log").open("x") as log_bench:
                    subprocess.run(bench_cmd, stdout=log_bench, stderr=subprocess.STDOUT,
                                   env=env, timeout=900, check=True)
            entry["status"] = "complete"
        except Exception:
            entry.update(status="failed", error=traceback.format_exc())
            raise
        finally:
            stop(server)
            entry["ended_utc"] = utc()
            write(RESULT / "suite-status.json", status)
            write(directory / "postflight.json", {
                "utc": utc(),
                "gpu_processes": subprocess.check_output(
                    ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                     "--format=csv,noheader"], text=True),
            })
            sync()

    try:
        for backend, executable in (
            ("native", ROOT / "venv/bin/python"), ("vllm", ROOT / "venv-vllm034/bin/python")
        ):
            data = json.loads(subprocess.check_output(
                [str(executable), str(ROOT / "scripts/vllm_single_environment.py")], text=True, env=env
            ))
            assert data["karpo_source"]["vcs_info"]["commit_id"] == manifest["code_revision"]
            assert data["model_weights_bytes"] == 9081227392
            if backend == "vllm":
                assert data["vllm"] == manifest["vllm_version"]
                for name, actual_hash in data["adapter_sha256"].items():
                    assert actual_hash == manifest["files"]["serving/vllm_karpo/" + name], name
            write(RESULT / f"environment-{backend}.json", data)
        gpu_log = (RESULT / "gpu.csv").open("x")
        monitor = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
             "--format=csv", "-l", "2"], stdout=gpu_log, stderr=subprocess.STDOUT, start_new_session=True
        )
        stage("native-initial", "native")
        for batch in (1, 4, 8):
            stage(f"vllm-b{batch}", "vllm", batch)
        selections = {}
        for scenario in ("risk", "sop"):
            choices = []
            for batch in (1, 4, 8):
                base = RESULT / "measurements" / f"vllm-b{batch}" / scenario
                quality = json.loads((base / "quality/summary.json").read_text())
                performance = json.loads((base / "sustained-100/summary.json").read_text())
                choices.append({"batch": batch, "quality": quality, "performance": performance})
            eligible = [c for c in choices if not c["quality"]["failures"]
                        and c["quality"]["accuracy_all_attempts"] > .90
                        and not c["performance"]["failures"]]
            if not eligible:
                raise RuntimeError(f"No vLLM configuration above 90% for {scenario}")
            chosen = min(eligible, key=lambda c: (c["performance"]["p50_ms"],
                                                  -c["performance"]["valid_rps"]))
            selections[scenario] = {"selected": chosen, "all_configs": choices}
            write(RESULT / "selection.json", selections)
        # One model startup can confirm both scenarios if the same setting won.
        if selections["risk"]["selected"]["batch"] == selections["sop"]["selected"]["batch"]:
            stage("vllm-confirm", "vllm", selections["risk"]["selected"]["batch"], confirm=True)
        else:
            for scenario in ("risk", "sop"):
                stage(f"vllm-confirm-{scenario}", "vllm", selections[scenario]["selected"]["batch"],
                      scenario=scenario, confirm=True)
        stage("native-confirm", "native", confirm=True)
        status["status"] = "complete"
    except Exception:
        status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        stop(monitor)
        if gpu_log:
            gpu_log.close()
        status["ended_utc"] = utc()
        write(RESULT / "suite-status.json", status)
        write(RESULT / "postflight.json", {
            "utc": utc(),
            "gpu_processes": subprocess.check_output(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv,noheader"], text=True),
            "model_weights_bytes": sum(p.stat().st_size for p in (ROOT / "models/karpo-4b").glob("*.safetensors")),
        })
        done.set()
        thread.join(timeout=35)
        sync()


if __name__ == "__main__":
    main()
