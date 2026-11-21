"""Deploy six identical model replicas and run one frozen fixed-batch protocol."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import shlex
import subprocess
import time
import traceback

import httpx

from promo_rtx6_runtime import EXP, ROOT, dump, synchronize, verify

PYTHON = "/opt/karpo/venv-vllm034/bin/python"
SSH = ["ssh", "-i", "/opt/karpo/.ssh/promo-rtx21", "-o", "BatchMode=yes",
       "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
       "-o", "UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts"]


def command(host, text, timeout=180):
    argv = ["bash", "-se"] if host["name"] == "dual" else SSH + [
        "ubuntu@" + host["ssh_address"], "sudo /bin/bash -se"]
    result = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{host['name']}: {result.stdout}\n{result.stderr}")
    return result.stdout


def collect(fleet):
    for host in fleet["hosts"]:
        if host["name"] == "dual":
            continue
        subprocess.run(["rsync", "-a", "-e", shlex.join(SSH), "--rsync-path=sudo rsync",
                        f"ubuntu@{host['ssh_address']}:{EXP}/results/nodes/",
                        str(EXP / "results/nodes") + "/"], check=True, timeout=180)
    synchronize()


def prepare_host(host, archive, digest):
    if host["name"] == "dual":
        return
    subprocess.run(["scp", *SSH[1:], archive, f"ubuntu@{host['ssh_address']}:{archive}"],
                   check=True, timeout=120)
    text = (
        f"printf '%s  %s\\n' {shlex.quote(digest)} {shlex.quote(archive)} | sha256sum -c -\n"
        f"mkdir -p {shlex.quote(str(ROOT))}\n"
        f"tar -xzf {shlex.quote(archive)} -C {shlex.quote(str(ROOT))}\n"
        f"ln -sfn /opt/karpo/models {shlex.quote(str(ROOT / 'models'))}\n"
    )
    if host["new"]:
        text += (f"mkdir -p {shlex.quote(str(EXP / 'results/setup'))}\n"
                 f"cp /opt/karpo/rtx6-bootstrap/ready.json {shlex.quote(str(EXP / 'results/setup' / (host['name']+'-weights.json')))}\n")
    command(host, text)


def main():
    verify("v1")
    fleet = json.loads((EXP / "fleet-v1.json").read_text())
    bundle = json.loads((EXP / "bundle-v1.json").read_text())
    target = EXP / "results/controller.json"
    if target.exists():
        raise RuntimeError("Controller already exists; do not overwrite measured evidence")
    state = {"status": "running", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    dump(target, state)
    try:
        # Bootstrap transfers complete before old model services are stopped.
        for host in fleet["hosts"]:
            if host["new"]:
                deadline = time.monotonic() + 2400
                while time.monotonic() < deadline:
                    try:
                        proof = json.loads(command(host, "cat /opt/karpo/rtx6-bootstrap/ready.json\n", 30))
                        assert proof["weights"] == fleet["expected_weights"]
                        dump(EXP / "results/setup" / (host["name"]+"-weights.json"), proof)
                        break
                    except Exception:
                        time.sleep(10)
                else:
                    raise RuntimeError("Bootstrap not ready: " + host["name"])
        state["current"] = "deploy"
        dump(target, state)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(prepare_host, h, bundle["remote_archive"], bundle["sha256"])
                    for h in fleet["hosts"]]
            for job in jobs:
                job.result()
        for host in fleet["hosts"]:
            if host.get("previous_unit"):
                unit = shlex.quote(host["previous_unit"])
                command(host, f"if systemctl is-active --quiet {unit}; then systemctl stop {unit}; fi\n")
        state["current"] = "models-starting"
        dump(target, state)
        for host in fleet["hosts"]:
            argv = [PYTHON, str(ROOT / "scripts/promo_rtx6_runtime.py"), "node",
                    "--name", host["name"], "--replica-ids", *map(str, host["replica_ids"]),
                    "--bind", "127.0.0.1" if host["cross_region"] else "0.0.0.0"]
            unit = "karpo-rtx6-model-" + host["name"]
            command(host, shlex.join(["systemd-run", "--unit", unit, "--property=Type=exec",
                "--property=TimeoutStopSec=90", "--property=RuntimeMaxSec=21600",
                "--working-directory", str(ROOT), *argv]) + "\n")
            if host["cross_region"]:
                tunnel = SSH + ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=3"]
                for local_gpu, replica in enumerate(host["replica_ids"]):
                    tunnel += ["-L", f"127.0.0.1:{18500+replica}:127.0.0.1:{18500+local_gpu}"]
                tunnel += ["ubuntu@" + host["ssh_address"]]
                subprocess.run(["systemd-run", "--unit", "karpo-rtx6-tunnel-" + host["name"],
                    "--property=Type=exec", "--property=RuntimeMaxSec=21600", *tunnel], check=True)
        deadline = time.monotonic() + 1200
        pending = set(enumerate(fleet["urls"]))
        with httpx.Client(timeout=4, trust_env=False) as client:
            while pending and time.monotonic() < deadline:
                for replica, url in list(pending):
                    try:
                        response = client.get(url + "/healthz")
                        response.raise_for_status()
                        info = response.json()
                        assert info["replica_id"] == replica
                        assert info["max_num_seqs"] == 4 and info["graph_policy"] == "powers"
                        pending.remove((replica, url))
                    except Exception:
                        pass
                if pending:
                    time.sleep(5)
        if pending:
            raise RuntimeError(f"Six-replica health check failed: {pending}")
        collect(fleet)
        state["current"] = "encode"
        dump(target, state)
        subprocess.run([PYTHON, str(ROOT / "scripts/promo_rtx6_runtime.py"), "encode"], check=True)
        state["current"] = "formal"
        dump(target, state)
        subprocess.run([PYTHON, str(ROOT / "scripts/promo_rtx6_runtime.py"), "run",
                        "--urls", *fleet["urls"]], check=True)
        state["status"] = "complete"
    except Exception:
        state.update(status="error", error=traceback.format_exc())
        raise
    finally:
        state["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump(target, state)
        # Model/SSH units and EC2 intentionally remain running.
        try:
            collect(fleet)
        except Exception:
            dump(EXP / "results/collection-error.json", {"error": traceback.format_exc()})
            synchronize()


if __name__ == "__main__":
    main()
