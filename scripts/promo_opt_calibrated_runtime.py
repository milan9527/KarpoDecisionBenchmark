"""Actual-model validation of the frozen promotional-risk fixtures on four RTX.

Node mode owns only its own model/sampler processes. Run mode owns the proxy.
Neither mode changes cloud state. Outputs never overwrite an existing run.
"""
import argparse
import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

import httpx

from promo_opt_calibration import evaluate as evaluate_responses
from promo_opt_calibrated_benchmark import Runner
from vllm_rps_benchmark import dump
from vllm_karpo_encoding import encoding_manifest

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/promo-opt-20261121"
BUCKET = "karpo-benchmark-20261119-115003-632930644527"


def verify(revision):
    manifest = json.loads((EXP / f"manifest-{revision}.json").read_text())
    for name, expected in manifest["files"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Frozen file changed: {name}")
    return manifest


def dataset(args):
    directory = EXP / "inputs" / args.dataset
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected, name
    return directory


def synchronize():
    """Incremental private backup from an instance with the existing S3 role."""
    import boto3
    client = boto3.client("s3", region_name="us-east-1")
    cache = EXP / ".upload-cache.json"
    previous = json.loads(cache.read_text()) if cache.exists() else {}
    changed = 0
    for path in [EXP / "encoding-reference.json", *sorted((EXP / "results").rglob("*"))]:
        if not path.is_file():
            continue
        key = str(path.relative_to(EXP))
        stat = path.stat()
        signature = [stat.st_size, stat.st_mtime_ns]
        if previous.get(key) == signature:
            continue
        client.upload_file(str(path), BUCKET, "results/" + EXP.name + "/" + key)
        previous[key] = signature
        changed += 1
    dump(cache, previous)
    return changed


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=15)


def health(url, processes=(), timeout=1200):
    start = time.monotonic()
    last = None
    with httpx.Client(timeout=10, trust_env=False) as client:
        while time.monotonic() - start < timeout:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("A model/proxy process exited; inspect its log")
            try:
                response = client.get(url + "/healthz")
                response.raise_for_status()
                return response.json()
            except Exception as error:
                last = str(error)
                time.sleep(2)
    raise RuntimeError(f"Health timeout for {url}: {last}")


def encode(args):
    verify(args.revision)
    data = dataset(args)
    target = EXP / "results/encodings" / (args.dataset + ".json")
    if target.exists():
        raise RuntimeError("Encoding baseline exists; use a new experiment for changed inputs")
    rows = encoding_manifest(args.checkpoint, data)
    ids = {row["request_id"] for row in rows}
    items = json.loads((dataset(args) / "risk-requests.json").read_text())
    assert ids == {row["id"] for row in items} and len(rows) == len(items)
    assert all(row["length"] <= 8192 and row["keys"] == ["action"] and row["counts"] == [6] for row in rows)
    expected = data / "expected-encoding.json"
    if expected.exists():
        assert rows == json.loads(expected.read_text()), "Encoding changed from previous run"
    dump(target, rows)
    dump(EXP / "results/encodings" / (args.dataset + "-summary.json"), {
        "requests": len(rows), "min_tokens": min(r["length"] for r in rows),
        "max_tokens": max(r["length"] for r in rows),
        "encoding_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "all_complete": True, "checkpoint": args.checkpoint,
    })
    print((EXP / "results/encodings" / (args.dataset + "-summary.json")).read_text(), flush=True)


def node(args):
    verify(args.revision)
    data = dataset(args)
    directory = EXP / "results/nodes" / args.name
    directory.mkdir(parents=True, exist_ok=False)
    dump(directory / "parameters.json", vars(args))
    hardware = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv"], text=True)
    (directory / "hardware.csv").write_text(hardware)
    rows = list(csv.reader(io.StringIO(hardware), skipinitialspace=True))[1:]
    if len(rows) != len(args.replica_ids) or any(
        "RTX PRO 6000 Blackwell Server Edition" not in row[1] or int(row[2].split()[0]) < 95000 for row in rows
    ):
        raise RuntimeError(f"Unexpected hardware: {hardware}")
    (directory / "environment.json").write_bytes(subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/vllm_single_environment.py")]))
    status = {"status": "starting", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "replica_ids": args.replica_ids}
    dump(directory / "status.json", status)
    processes, logs = [], []
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        monitor = (directory / "gpu.csv").open("x")
        logs.append(monitor)
        processes.append(subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
             "--format=csv", "-l", "2"], stdout=monitor, stderr=subprocess.STDOUT, start_new_session=True))
        urls, models = [], []
        for gpu, replica in enumerate(args.replica_ids):
            out = directory / f"replica-{replica}"
            out.mkdir()
            port = 18500 + gpu
            command = [
                sys.executable, str(ROOT / "scripts/promo_opt_server.py"),
                "--checkpoint", args.checkpoint, "--experiment", str(data),
                "--output", str(out), "--max-seqs", str(args.batch), "--replica-id", str(replica),
                "--host", args.bind, "--port", str(port), "--admission-limit", "64", "--graph", args.graph,
            ]
            dump(out / "command.json", command)
            log = (out / "server.log").open("x")
            logs.append(log)
            env = {**os.environ, "CUDA_VISIBLE_DEVICES": str(gpu), "OMP_NUM_THREADS": "4",
                   "TOKENIZERS_PARALLELISM": "false", "PYTHONUNBUFFERED": "1",
                   "TORCHINDUCTOR_COMPILE_THREADS": "2"}
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(process)
            models.append(process)
            urls.append(f"http://127.0.0.1:{port}")
        for replica, url in zip(args.replica_ids, urls, strict=True):
            info = health(url, models)
            assert info["replica_id"] == replica and info["max_num_seqs"] == args.batch
            dump(directory / f"replica-{replica}/health.json", info)
        status.update(status="ready", ready_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        dump(directory / "status.json", status)
        start = time.monotonic()
        while not stopping and time.monotonic() - start < args.node_seconds:
            if any(p.poll() is not None for p in models):
                raise RuntimeError("A replica exited")
            time.sleep(2)
        status["status"] = "stopped"
    except Exception:
        status.update(status="error", error=traceback.format_exc())
        raise
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        status["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump(directory / "status.json", status)


async def run(args):
    manifest = verify(args.revision)
    extra = json.loads((EXP / "calibration-runtime-v1.json").read_text())
    for name, expected in extra["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    directory = EXP / "results/runs" / args.name
    directory.mkdir(parents=True, exist_ok=False)
    dump(directory / "manifest.json", manifest)
    dump(directory / "parameters.json", vars(args))
    items = json.loads((dataset(args) / "risk-requests.json").read_text())
    quality = [r for r in items if r["group"] == "quality"]
    tune = [r for r in items if r["group"] == "tune"]
    encodings = {r["request_id"]: r for r in json.loads((EXP / "results/encodings" / (args.dataset + ".json")).read_text())}
    assert set(encodings) == {r["id"] for r in items}
    assert len(args.urls) == 4
    status = {"status": "running", "stages": [], "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "dataset": args.dataset, "protocol": args.protocol, "graph": args.graph, "gpu_count": 4, "batch": args.batch, "decision_policy": "review-threshold-v1"}
    dump(directory / "status.json", status)
    proxy_log = (directory / "proxy.log").open("x")
    proxy = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/promo_opt_calibrated_proxy.py"), "--urls", *args.urls, "--port", "18770"],
        stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        info = health("http://127.0.0.1:18770", [proxy])
        assert len(info["backends"]) == 4
        settings = []
        for i, backend in enumerate(info["backends"]):
            assert backend["replica_id"] == i and backend["max_num_seqs"] == args.batch
            assert backend["settings"]["quantization"] == "fp8_per_channel"
            settings.append(backend["settings"])
        assert all(value == settings[0] for value in settings)
        dump(directory / "health.json", info)
        stages = {
            "formal": [("warmup", tune[:80], None, None), ("quality", quality, None, None),
                       ("low-load", tune, 40, 20), ("probe", tune, 100, 20),
                       ("confirm", tune, 100, 120), ("repeat", tune, 100, 120)],
            "scan": [("tune", tune, None, None), ("low-load", tune, 40, 10), ("probe", tune, 100, 30)],
            "screen": [("warmup", tune[:16], None, None), ("tune", tune, None, None)],
            "quality-only": [("warmup", tune[:16], None, None), ("quality", quality, None, None)],
        }[args.protocol]
        for name, workload, rate, seconds in stages:
            status["current"] = name
            dump(directory / "status.json", status)
            target = directory / name
            wall = dt.datetime.now(dt.timezone.utc)
            anchor = time.perf_counter()
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:18770", timeout=120, trust_env=False,
                limits=httpx.Limits(max_connections=2048, max_keepalive_connections=2048),
            ) as client:
                runner = Runner(client, target, encodings)
                if rate is None:
                    result = await runner.quality(workload, concurrency=4)
                else:
                    result = await runner.fixed_rate(workload, rate, seconds)
            started = wall + dt.timedelta(seconds=runner.begin-anchor)
            dump(target / "timing.json", {
                "begin_utc": started.isoformat(), "end_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "offered_end_utc": (started+dt.timedelta(seconds=seconds)).isoformat() if seconds else None,
            })
            if name == "quality" and "behavior" in items[0]:
                dump(target / "quality-details.json", evaluate_responses(items, runner.rows))
            status["stages"].append({"name": name, "result": result})
            dump(directory / "status.json", status)
            print(json.dumps({"stage": name, "result": {k:v for k,v in result.items() if k!="backlog_samples"}}), flush=True)
            # Continue performance measurement even if quality is below target.
            # Failed quality must stay visible and cannot be rescued by throughput.
            if not args.no_upload:
                synchronize()
        measured = {s["name"]: s["result"] for s in status["stages"]}
        if args.protocol == "formal":
            status["quality_passed"] = measured["quality"]["accuracy"] >= 0.9 and not measured["quality"]["failures"]
            status["performance_passed"] = all(measured[s]["passes_performance_acceptance"] for s in ("confirm", "repeat"))
            status["pressure_quality_passed"] = all(measured[s]["correct"] / measured[s]["attempts"] >= 0.9 for s in ("confirm", "repeat"))
            status["accepted"] = status["quality_passed"] and status["performance_passed"] and status["pressure_quality_passed"]
        status["status"] = "complete"
    except Exception:
        status.update(status="error", error=traceback.format_exc())
        raise
    finally:
        stop(proxy)
        proxy_log.close()
        status["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump(directory / "status.json", status)
        if not args.no_upload:
            synchronize()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["encode", "node", "run", "sync"])
    parser.add_argument("--dataset", default="old")
    parser.add_argument("--graph", choices=["powers", "dense"], default="powers")
    parser.add_argument("--protocol", choices=["formal", "scan", "screen", "quality-only"], default="formal")
    parser.add_argument("--revision", default="v1")
    parser.add_argument("--checkpoint", default="/opt/karpo/models/karpo-4b")
    parser.add_argument("--name", default="rtx4-fp8-v1")
    parser.add_argument("--batch", type=int, choices=[1,2,4,8,16], default=4)
    parser.add_argument("--replica-ids", type=int, nargs="+", default=[0])
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--node-seconds", type=int, default=21600)
    parser.add_argument("--urls", nargs="+")
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()
    if args.mode == "encode":
        encode(args)
    elif args.mode == "node":
        node(args)
    elif args.mode == "run":
        asyncio.run(run(args))
    else:
        print(synchronize())
