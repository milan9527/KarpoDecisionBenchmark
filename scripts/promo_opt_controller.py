"""Sequential four-GPU experiments. Owns model services, never EC2 state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

from promo_opt_runtime import EXP, ROOT, dump, synchronize, verify

PYTHON = "/opt/karpo/venv-vllm034/bin/python"
PEERS = {"dual": None, "peer2": "172.31.42.209", "peer3": "172.31.37.239"}
URLS = ["http://127.0.0.1:18500", "http://127.0.0.1:18501",
        "http://172.31.42.209:18500", "http://172.31.37.239:18500"]
SSH = ["ssh", "-i", "/opt/karpo/.ssh/promo-rtx21", "-o", "BatchMode=yes",
       "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
       "-o", "UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts"]


def command(host, text):
    args = ["bash", "-se"] if host == "dual" else SSH + ["ubuntu@" + PEERS[host], "sudo /bin/bash -se"]
    result = subprocess.run(args, input=text, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"{host}: {result.stdout}\n{result.stderr}")
    return result.stdout


def parallel(commands):
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(command, host, text) for host, text in commands.items()]
        return [job.result() for job in jobs]


def collect():
    # Copy node telemetry after measurement, to avoid backup traffic during load.
    for host, address in PEERS.items():
        if not address:
            continue
        subprocess.run(["rsync", "-a", "-e", shlex.join(SSH), "--rsync-path=sudo rsync",
                        f"ubuntu@{address}:{EXP}/results/nodes/", str(EXP / "results/nodes") + "/"],
                       check=True, timeout=120)
    synchronize()


def launch(name, dataset, batch, graph):
    previous = EXP / "results/current-fleet.json"
    units = json.loads(previous.read_text())["units"] if previous.exists() else {
        "dual": "karpo-promo-dual-v1", "peer2": "karpo-promo-peer2-v1", "peer3": "karpo-promo-peer3-v1"}
    parallel({host: f"systemctl stop {shlex.quote(unit)}\n" for host, unit in units.items()})
    time.sleep(3)
    units = {host: f"karpo-opt-{name}-{host}" for host in PEERS}
    dump(previous, {"name": name, "dataset": dataset, "batch": batch, "graph": graph, "units": units})
    commands = {}
    for host, unit in units.items():
        ids = ["0", "1"] if host == "dual" else [host[-1]]
        argv = [PYTHON, str(ROOT / "scripts/promo_opt_runtime.py"), "node", "--dataset", dataset,
                "--name", name + "-" + host, "--batch", str(batch), "--graph", graph,
                "--replica-ids", *ids, "--bind", "0.0.0.0"]
        commands[host] = shlex.join(["systemd-run", "--unit", unit, "--property=Type=exec",
                                    "--property=TimeoutStopSec=90", "--property=RuntimeMaxSec=21600",
                                    "--working-directory", str(ROOT), *argv]) + "\n"
    parallel(commands)
    import httpx
    deadline = time.monotonic() + 1200
    pending = set(URLS)
    with httpx.Client(timeout=3, trust_env=False) as client:
        while pending and time.monotonic() < deadline:
            for url in list(pending):
                try:
                    response = client.get(url + "/healthz")
                    response.raise_for_status()
                    info = response.json()
                    assert info["max_num_seqs"] == batch and info["graph_policy"] == graph
                    pending.remove(url)
                except Exception:
                    pass
            if pending:
                time.sleep(5)
    if pending:
        collect()
        raise RuntimeError(f"Models not ready: {pending}")
    collect()


def run(name, dataset, batch, graph, protocol):
    if not (EXP / "results/encodings" / (dataset + ".json")).exists():
        subprocess.run([PYTHON, str(ROOT / "scripts/promo_opt_runtime.py"), "encode",
                        "--dataset", dataset], check=True)
    subprocess.run([PYTHON, str(ROOT / "scripts/promo_opt_runtime.py"), "run", "--name", name,
                    "--dataset", dataset, "--batch", str(batch), "--graph", graph,
                    "--protocol", protocol, "--urls", *URLS], check=True)
    collect()
    return json.loads((EXP / "results/runs" / name / "status.json").read_text())


def performance_selection(rows):
    candidates = []
    for row in rows:
        probe = next(s["result"] for s in row["stages"] if s["name"] == "probe")
        candidates.append({"name": row["name"], "batch": row["batch"], "graph": row["graph"],
                           "probe": probe})
    eligible = [c for c in candidates if c["probe"]["passes_probe"] and not c["probe"]["failures"]]
    ranked = sorted(eligible, key=lambda c: (c["probe"]["p50_ms"], c["batch"], c["graph"])) if eligible else sorted(
        candidates, key=lambda c: (-c["probe"]["steady_valid_rps"], c["probe"]["p50_ms"], c["batch"]))
    return {"selected": ranked[0], "candidates": candidates, "any_probe_passed": bool(eligible),
            "selection_basis": "100-RPS tune probe only; quality holdout not used",
            "frozen_utc": dt.datetime.now(dt.timezone.utc).isoformat()}


def main(phase):
    verify("v1")
    path = EXP / "results/controllers" / (phase + ".json")
    if path.exists():
        raise RuntimeError("Phase already exists; do not overwrite experiment evidence")
    state = {"phase": phase, "status": "running", "completed": [],
             "started_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    dump(path, state)
    try:
        if phase == "environment":
            launch("old-b4-powers", "old", 4, "powers")
            run("old-control", "old", 4, "powers", "formal")
            state["completed"].append("old-control")
        elif phase == "performance":
            rows = []
            for batch in (4, 8, 16):
                for graph in ("powers", "dense"):
                    name = f"original-b{batch}-{graph}"
                    state["current"] = name
                    dump(path, state)
                    launch(name, "original", batch, graph)
                    row = run(name, "original", batch, graph, "scan")
                    row["name"] = name
                    rows.append(row)
                    state["completed"].append(name)
                    dump(path, state)
            selection = performance_selection(rows)
            dump(EXP / "results/performance-selection.json", selection)
            best = selection["selected"]
            launch("original-selected", "original", best["batch"], best["graph"])
            run("original-confirmed", "original", best["batch"], best["graph"], "formal")
            state["completed"].append("original-confirmed")
        state["status"] = "complete"
    except Exception:
        import traceback
        state.update(status="error", error=traceback.format_exc())
        raise
    finally:
        state["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump(path, state)
        collect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["environment", "performance"])
    main(parser.parse_args().phase)
