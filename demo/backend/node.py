"""TLS gateway and process supervisor for the existing Karpo GPU replicas."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import time

import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
import uvicorn

ROOT = Path("/opt/karpo-demo")
config = json.loads((ROOT / "node.json").read_text())
secret = json.loads(boto3.client("secretsmanager", region_name="us-east-2").get_secret_value(
    SecretId=config["secret"])["SecretString"])
for name, key in (("node.crt", "cert"), ("node.key", "key")):
    path = ROOT / name
    path.write_text(secret[key])
    path.chmod(0o600)
processes, logs = [], []
run = ROOT / "node-runs" / str(time.time_ns())
run.mkdir(parents=True)


@asynccontextmanager
async def lifespan(app):
    script = "promo_opt_server.py" if config["group"] == "risk" else "vllm_rps_server.py"
    experiment = ("experiments/promo-rtx6-20261121/inputs/review-baseline" if config["group"] == "risk"
                  else "experiments/vllm-sop-h200-20261120")
    env = {**os.environ, "OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1", "TORCHINDUCTOR_COMPILE_THREADS": "2",
           "PYTHONPATH": str(ROOT / "serving/vllm_karpo") + ":" + str(ROOT / "scripts")}
    for gpu, replica in enumerate(config["replicas"]):
        output = run / f"gpu-{gpu}"
        output.mkdir()
        log = (output / "server.log").open("w")
        logs.append(log)
        args = ["/opt/karpo/venv-vllm034/bin/python", str(ROOT / "scripts" / script),
                "--checkpoint", "/opt/karpo/models/karpo-4b", "--experiment", str(ROOT / experiment),
                "--output", str(output), "--max-seqs", "4", "--replica-id", str(replica),
                "--host", "127.0.0.1", "--port", str(19500 + gpu), "--admission-limit", "16"]
        if config["group"] == "risk":
            args += ["--graph", "powers"]
        processes.append(subprocess.Popen(args, env={**env, "CUDA_VISIBLE_DEVICES": str(gpu)},
                                          stdout=log, stderr=subprocess.STDOUT))
    app.state.client = httpx.AsyncClient(timeout=120, trust_env=False)
    async def supervise():
        while True:
            await asyncio.sleep(5)
            if any(process.poll() is not None for process in processes):
                # systemd KillMode=control-group clears the remaining replicas
                # and Restart=on-failure restarts this node as a complete unit.
                print("A model process exited; restarting the GPU service.", flush=True)
                os._exit(1)
    monitor = asyncio.create_task(supervise())
    yield
    monitor.cancel()
    await app.state.client.aclose()
    for process in processes:
        process.terminate()
    for log in logs:
        log.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def authenticate(request, call_next):
    import hmac
    if not hmac.compare_digest(request.headers.get("X-Node-Token", ""), secret["token"]):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health():
    async def probe(gpu, replica):
        ready = False
        try:
            r = await app.state.client.get(f"http://127.0.0.1:{19500+gpu}/healthz", timeout=2)
            value = r.json()
            ready = (r.status_code == 200 and value["replica_id"] == replica and
                     value["max_num_seqs"] == 4 and processes[gpu].poll() is None)
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return {"replica": replica, "ready": ready, "process_alive": processes[gpu].poll() is None}
    return {"replicas": config["replicas"], "models": await asyncio.gather(
        *(probe(gpu, replica) for gpu, replica in enumerate(config["replicas"])))}


@app.post("/replicas/{gpu}/infer")
async def infer(gpu: int, request: Request):
    if gpu not in range(len(config["replicas"])):
        raise HTTPException(404)
    body = await request.body()
    if len(body) > 262144:
        raise HTTPException(413)
    response = await app.state.client.post(f"http://127.0.0.1:{19500+gpu}/v1/systemone",
        content=body, headers={"Content-Type": "application/json"})
    return JSONResponse(response.json(), status_code=response.status_code)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=19443, ssl_keyfile=str(ROOT / "node.key"),
                ssl_certfile=str(ROOT / "node.crt"), access_log=False)
