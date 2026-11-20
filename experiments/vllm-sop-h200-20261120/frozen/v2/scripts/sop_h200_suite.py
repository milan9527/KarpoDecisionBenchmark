"""Homogeneous H200 SOP sweep with fixed arrivals and independent confirmations."""
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

from vllm_rps_benchmark import Runner, dump
from vllm_rps_suite import stop, wait_health

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-sop-h200-20261120"


def acceptance(result):
    return (
        result.get("passes_performance_acceptance", False)
        and result["correct"] / result["attempts"] > .9
    )


class Suite:
    def __init__(self, args):
        self.args = args
        self.directory = EXP / "results/runs" / args.name
        self.directory.mkdir(parents=True, exist_ok=False)
        manifest = json.loads((EXP / f"manifest-{args.revision}.json").read_text())
        for name, expected in manifest["files"].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Frozen source/input changed: {name}")
        dump(self.directory / "manifest.json", manifest)
        dump(self.directory / "parameters.json", vars(args))
        items = json.loads((EXP / "sop-requests.json").read_text())
        self.quality = [i for i in items if i["group"] == "quality"]
        self.performance = [i for i in items if i["group"] == "tune"] or items
        assert len(self.quality) == 104
        self.encodings = {
            i["request_id"]: i for i in json.loads((EXP / "encoding-reference.json").read_text())
        }
        self.status = {
            "status": "running", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scenario": "sop", "gpu_model": "H200", "ec2_policy": "keep_running", "stages": [],
            "candidates": [], "confirmed": False,
        }
        self.processes = []
        self.logs = []
        self.monitor = None
        self.monitor_log = None
        self.active_batch = None
        self.proxy_url = "http://127.0.0.1:18750"
        self.env = {
            **os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
            "PYTHONUNBUFFERED": "1", "TORCHINDUCTOR_COMPILE_THREADS": "2",
        }
        self.save()

    def save(self):
        dump(self.directory / "status.json", self.status)

    def inspect(self):
        hardware = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv"], text=True)
        (self.directory / "gpu-hardware.csv").write_text(hardware)
        rows = list(csv.DictReader(io.StringIO(hardware), skipinitialspace=True))
        if len(rows) != self.args.gpus or any("H200" not in r["name"] for r in rows):
            raise RuntimeError(f"Expected exactly {self.args.gpus} H200 GPUs")
        (self.directory / "gpu-topology.txt").write_text(subprocess.check_output(
            ["nvidia-smi", "topo", "-m"], text=True))
        (self.directory / "environment.json").write_bytes(subprocess.check_output(
            [sys.executable, str(ROOT / "scripts/vllm_single_environment.py")], env=self.env))
        self.monitor_log = (self.directory / "gpu.csv").open("x")
        self.monitor = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw",
             "--format=csv", "-l", "2"],
            stdout=self.monitor_log, stderr=subprocess.STDOUT, start_new_session=True)

    def shutdown_models(self):
        # Send signals together so each GPU begins cleanup without serial delays.
        for process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in reversed(self.processes):
            stop(process)
        self.processes.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()
        self.active_batch = None

    def start_models(self, batch, phase):
        self.shutdown_models()
        self.status["current"] = {"phase": phase, "batch": batch, "stage": "starting_models"}
        self.save()
        directory = self.directory / phase / f"b{batch}"
        directory.mkdir(parents=True, exist_ok=False)
        urls = []
        servers = []
        for gpu in range(self.args.gpus):
            out = directory / f"server-{gpu}"
            out.mkdir()
            url = f"http://127.0.0.1:{18500 + gpu}"
            urls.append(url)
            command = [
                sys.executable, str(ROOT / "scripts/vllm_rps_server.py"),
                "--checkpoint", self.args.checkpoint, "--experiment", str(EXP),
                "--output", str(out), "--max-seqs", str(batch), "--replica-id", str(gpu),
                "--port", str(18500 + gpu), "--admission-limit", "64",
            ]
            dump(out / "command.json", command)
            log = (out / "server.log").open("x")
            self.logs.append(log)
            process = subprocess.Popen(
                command, env={**self.env, "CUDA_VISIBLE_DEVICES": str(gpu)},
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            servers.append(process)
            self.processes.append(process)
        for gpu, (server, url) in enumerate(zip(servers, urls, strict=True)):
            health = wait_health(server, url, limit=1500)
            assert health["replica_id"] == gpu
            dump(directory / f"server-{gpu}/health.json", health)
        log = (directory / "proxy.log").open("x")
        self.logs.append(log)
        proxy = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/vllm_rps_proxy.py"),
             "--urls", *urls, "--port", "18750"],
            env=self.env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(proxy)
        dump(directory / "proxy-health.json", wait_health(proxy, self.proxy_url))
        self.active_batch = batch

    def configure(self, replicas):
        response = httpx.post(self.proxy_url + "/configure",
                              json={"active": list(range(replicas))}, timeout=10, trust_env=False)
        response.raise_for_status()

    async def measure(self, batch, replicas, phase, stage, rate=None, seconds=None):
        entry = {"phase": phase, "batch": batch, "replicas": replicas, "stage": stage}
        self.status["current"] = entry
        self.save()
        target = self.directory / phase / f"b{batch}/r{replicas}/sop" / stage
        perf_anchor = time.perf_counter()
        wall_anchor = dt.datetime.now(dt.timezone.utc)
        limits = httpx.Limits(max_connections=2048, max_keepalive_connections=2048)
        async with httpx.AsyncClient(
            base_url=self.proxy_url, timeout=120, trust_env=False, limits=limits
        ) as client:
            runner = Runner(client, target, self.encodings)
            if rate is None:
                result = await runner.quality(self.quality, replicas)
            else:
                result = await runner.fixed_rate(self.performance, rate, seconds)
            started = wall_anchor + dt.timedelta(seconds=runner.begin - perf_anchor)
            dump(target / "timing.json", {
                "begin_utc": started.isoformat(), "end_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "offered_end_utc": (started + dt.timedelta(seconds=seconds)).isoformat() if seconds else None,
                "clock": "UTC paired with time.perf_counter; request offsets use runner.begin",
            })
        record = {**entry, "result": result}
        self.status["stages"].append(record)
        self.save()
        print(json.dumps({**entry, "result": {k: v for k, v in result.items() if k != "backlog_samples"}}),
              flush=True)
        return result

    def run(self):
        self.inspect()
        for batch in self.args.batches:
            self.start_models(batch, "sweep")
            for replicas in self.args.replicas:
                self.configure(replicas)
                quality = asyncio.run(self.measure(batch, replicas, "sweep", "quality"))
                if quality["failures"] or quality["accuracy"] <= .9:
                    continue
                asyncio.run(self.measure(batch, replicas, "sweep", "low-load", 8 * replicas, 20))
                probe = asyncio.run(self.measure(batch, replicas, "sweep", "rps100", 100, 20))
                if probe["passes_probe"] and probe["correct"] / probe["attempts"] > .9:
                    self.status["candidates"].append({
                        "batch": batch, "replicas": replicas, "probe_p50_ms": probe["p50_ms"]})
                    self.save()
        if self.status["candidates"]:
            selected = min(self.status["candidates"], key=lambda x: (x["replicas"], x["probe_p50_ms"]))
            self.status["selected"] = selected
            self.save()
            batch, replicas = selected["batch"], selected["replicas"]
            if self.active_batch != batch:
                self.start_models(batch, "confirmation")
            self.configure(replicas)
            quality = asyncio.run(self.measure(batch, replicas, "confirmation", "quality"))
            if not quality["failures"] and quality["accuracy"] > .9:
                results = [asyncio.run(self.measure(batch, replicas, "confirmation", stage, 100, 120))
                           for stage in ("confirm", "repeat")]
                self.status["confirmed"] = all(acceptance(r) for r in results)
        self.status["status"] = "complete"

    def finish(self):
        self.shutdown_models()
        stop(self.monitor)
        if self.monitor_log:
            self.monitor_log.close()
        self.status["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--revision", default="v1")
    parser.add_argument("--checkpoint", default=str(ROOT / "models/karpo-4b"))
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--replicas", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 4])
    args = parser.parse_args()
    assert 1 <= min(args.replicas) <= max(args.replicas) <= args.gpus
    assert all(b in (1, 2, 4, 8) for b in args.batches)
    suite = Suite(args)

    def interrupted(signum, frame):
        raise InterruptedError(f"Signal {signum}; retain EC2 and preserve partial measurements")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        suite.run()
    except BaseException:
        suite.status.update(status="failed", error=traceback.format_exc())
        raise
    finally:
        suite.finish()


if __name__ == "__main__":
    main()
