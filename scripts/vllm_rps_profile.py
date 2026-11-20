"""Collect separate GPU traces for original-input risk and SOP on one GPU."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx

from benchmark import validate
from vllm_rps_suite import stop, wait_health

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments/vllm-rps100-20261120"
OUT = EXP / "results/profile-b1"


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    for name in ("vllm_rps_profile.py", "vllm_rps_profile_server.py"):
        shutil.copyfile(ROOT / "scripts" / name, OUT / name)
    command = [
        sys.executable, str(ROOT / "scripts/vllm_rps_profile_server.py"),
        "--checkpoint", str(ROOT / "models/karpo-4b"), "--output", str(OUT / "server"),
        "--profile-dir", str(OUT / "traces"), "--max-seqs", "1", "--port", "18600",
    ]
    (OUT / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    process = None
    try:
        with (OUT / "server.log").open("x") as log:
            process = subprocess.Popen(
                command, env={**os.environ, "CUDA_VISIBLE_DEVICES": "0", "OMP_NUM_THREADS": "4",
                              "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false"},
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            wait_health(process, "http://127.0.0.1:18600")
            with httpx.Client(base_url="http://127.0.0.1:18600", timeout=180, trust_env=False) as client:
                for scenario in ("risk", "sop"):
                    items = json.loads((EXP / f"{scenario}-requests.json").read_text())
                    items = [i for i in items if i["group"] == "tune"] or items
                    for item in items[:16]:
                        response = client.post("/v1/systemone", json=item["request"])
                        response.raise_for_status()
                        validate(response.json(), item)
                    response = client.post("/start_profile", json={"prefix": f"{scenario}-b1"})
                    response.raise_for_status()
                    rows = []
                    for item in items[:16]:
                        response = client.post("/v1/systemone", json=item["request"])
                        response.raise_for_status()
                        body = response.json()
                        rows.append({"request_id": item["id"], "response": body, "correct": validate(body, item)})
                    response = client.post("/stop_profile")
                    response.raise_for_status()
                    (OUT / f"{scenario}-responses.json").write_text(json.dumps(rows, ensure_ascii=False) + "\n")
        (OUT / "complete.json").write_text('{"status":"complete","purpose":"diagnostic traces; not latency acceptance"}\n')
    finally:
        stop(process)
        subprocess.run([
            "aws", "s3", "cp", str(OUT) + "/",
            "s3://karpo-benchmark-20261119-115003-632930644527/results/vllm-rps100-20261120/profile-b1/",
            "--recursive", "--region", "us-east-1", "--no-progress",
        ], check=False)


if __name__ == "__main__":
    main()
