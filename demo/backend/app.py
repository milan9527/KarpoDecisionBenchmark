"""Authenticated demo API, durable readiness gating and GPU startup control."""
import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import secrets
import ssl
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx

from state import Conflict, Store, acquire, activity, release, begin_request
from auth import COOKIE_SECONDS, LoginLimiter, issue_session, read_session, verify_credentials, viewer_ip

ROOT = Path(os.environ.get("DEMO_ROOT", "/opt/karpo-demo"))
CONFIG = json.loads((ROOT / "demo/config.json").read_text())
RESOURCES = json.loads((ROOT / "resources.json").read_text())
REGION = CONFIG["region"]
SDK = Config(connect_timeout=4, read_timeout=15, retries={"max_attempts": 2})
store = Store(boto3.resource("dynamodb", region_name=REGION).Table(RESOURCES["table"]))
secret = json.loads(boto3.client("secretsmanager", region_name=REGION).get_secret_value(
    SecretId=RESOURCES["backend_secret"])["SecretString"])
certificate = ROOT / "node-ca.crt"
certificate.write_text(secret["node_cert"])
tls = ssl.create_default_context(cafile=str(certificate))
tls.check_hostname = False  # Trust only the private demo certificate; node addresses change at restart.
catalog = json.loads((ROOT / "demo/catalog.json").read_text())
busy = {}
login_limiter = LoginLimiter()
password_workers = asyncio.Semaphore(2)
quitting = threading.Event()


def sdk(service, region=REGION):
    return boto3.client(service, region_name=region, config=SDK)


def instances(group):
    result = {}
    for region in {h["region"] for h in CONFIG["groups"][group]["hosts"]}:
        ids = [h["id"] for h in CONFIG["groups"][group]["hosts"] if h["region"] == region]
        for reservation in sdk("ec2", region).describe_instances(InstanceIds=ids)["Reservations"]:
            for host in reservation["Instances"]:
                result[host["InstanceId"]] = host
    return result


def address(host, region):
    return host["PrivateIpAddress"] if region == REGION else host.get("PublicIpAddress")


def patch_generation(group, generation, changes, phases=("starting",)):
    def transform(old):
        if old["generation"] != generation or old["phase"] not in phases:
            return None
        old.update(changes)
        return old
    return store.update(group, transform)


def reconcile(group):
    value = store.get(group)
    if value["phase"] not in ("starting", "ready"):
        return
    generation = value["generation"]
    hardware = instances(group)
    if value["phase"] == "starting":
        if int(time.time()) >= value["startup_deadline"]:
            return
        for region in {h["region"] for h in CONFIG["groups"][group]["hosts"]}:
            stopped = [h["id"] for h in CONFIG["groups"][group]["hosts"]
                       if h["region"] == region and hardware[h["id"]]["State"]["Name"] == "stopped"]
            if stopped:
                current = store.get(group)
                if current["phase"] != "starting" or current["generation"] != generation:
                    return
                sdk("ec2", region).start_instances(InstanceIds=stopped)
    hosts, ready = [], 0
    with httpx.Client(verify=tls, timeout=3, trust_env=False,
                     headers={"X-Node-Token": secret["node_token"]}) as client:
        for spec in CONFIG["groups"][group]["hosts"]:
            host = hardware[spec["id"]]
            row = {"id": spec["id"], "region": spec["region"], "name": spec["name"],
                   "state": host["State"]["Name"], "gpus": len(spec["replicas"]),
                   "ready": 0, "message": "等待实例启动", "address": address(host, spec["region"]) or ""}
            if row["state"] == "running":
                if value["phase"] == "starting" and spec["id"] not in value.get("commands", {}):
                    online = sdk("ssm", spec["region"]).describe_instance_information(
                        Filters=[{"Key": "InstanceIds", "Values": [spec["id"]]}])["InstanceInformationList"]
                    if online and online[0]["PingStatus"] == "Online":
                        current = store.get(group)
                        if current["generation"] != generation or current["phase"] != "starting":
                            return
                        command = sdk("ssm", spec["region"]).send_command(
                            InstanceIds=[spec["id"]], DocumentName="AWS-RunShellScript",
                            Parameters={"commands": [
                                "set -eu\nsystemctl stop 'karpo-rtx6-model-*' 'karpo-opt-review-selected-*' || true\n"
                                "systemctl start karpo-demo-node\nsystemctl is-active karpo-demo-node"],
                                "executionTimeout": ["60"]}, TimeoutSeconds=90)["Command"]["CommandId"]
                        commands = {**current.get("commands", {}), spec["id"]: command}
                        patch_generation(group, generation, {"commands": commands})
                        value["commands"] = commands
                row["message"] = "加载模型与CUDA Graph"
                command_id = value.get("commands", {}).get(spec["id"])
                if command_id:
                    try:
                        command_result = sdk("ssm", spec["region"]).get_command_invocation(
                            CommandId=command_id, InstanceId=spec["id"])
                        if command_result["Status"] in ("Failed", "Cancelled", "TimedOut"):
                            raise RuntimeError(f"{spec['name']} GPU服务启动失败，请检查该节点的服务安装记录。")
                    except ClientError as error:
                        if error.response["Error"]["Code"] != "InvocationDoesNotExist":
                            raise
                try:
                    response = client.get(f"https://{row['address']}:19443/health")
                    response.raise_for_status()
                    health = response.json()
                    assert health["replicas"] == spec["replicas"]
                    row["ready"] = sum(h.get("ready", False) for h in health["models"])
                    row["message"] = "模型已就绪" if row["ready"] == row["gpus"] else "加载模型与CUDA Graph"
                except (httpx.HTTPError, AssertionError, ValueError):
                    pass
            ready += row["ready"]
            hosts.append(row)
    now = int(time.time())
    changes = {"hosts": hosts, "ready_gpus": ready, "checked_at": now}
    if ready == CONFIG["groups"][group]["gpu_count"]:
        if value["phase"] == "starting":
            changes.update(phase="ready", ready_at=now, stop_after=max(
                [now + 600] + list(store.get(group)["sessions"].values())))
    elif value["phase"] == "ready":
        changes.update(phase="error", error="部分GPU模型服务不再就绪，请重新启动演示。",
                       stop_after=min(value["stop_after"], now + 600))
    patch_generation(group, generation, changes, phases=("starting", "ready"))


def control_loop():
    while not quitting.is_set():
        for group in CONFIG["groups"]:
            try:
                reconcile(group)
            except Exception as error:
                try:
                    value = store.get(group)
                    patch_generation(group, value["generation"], {
                        "phase": "error", "error": str(error)[:800], "stop_after": int(time.time()) + 600},
                        phases=("starting", "ready"))
                except Exception:
                    pass
                print(json.dumps({"group": group, "control_error": str(error)}, ensure_ascii=False), flush=True)
        quitting.wait(5)


@asynccontextmanager
async def lifespan(app):
    worker = threading.Thread(target=control_loop, daemon=True)
    worker.start()
    app.state.client = httpx.AsyncClient(verify=tls, timeout=120, trust_env=False,
                                         headers={"X-Node-Token": secret["node_token"]})
    yield
    quitting.set()
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(Conflict)
async def conflict(request, error):
    return JSONResponse({"detail": str(error)}, status_code=409)


@app.middleware("http")
async def protect(request, call_next):
    if request.method == "POST":
        if request.headers.get("origin") not in (None, "https://" + RESOURCES.get("domain", "")):
            return JSONResponse({"detail": "来源不允许"}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"detail": "需要JSON请求"}, status_code=415)
        if int(request.headers.get("content-length", "0")) > 262144:
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        body = await request.body()
        if len(body) > 262144:
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        try:
            if not isinstance(json.loads(body), dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"detail": "请输入有效的JSON对象"}, status_code=422)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def authenticated(request):
    try:
        return read_session(request.cookies.get("karpo_demo"), secret["cookie_key"], secret["account"]["username"])
    except ValueError:
        raise HTTPException(401, "请先使用用户名和密码登录") from None


def group_name(group):
    if group not in CONFIG["groups"]:
        raise HTTPException(404, "未知演示场景")
    return group


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/api/auth")
async def login(request: Request):
    ip = viewer_ip(request.headers.get("x-forwarded-for", ""), request.client.host)
    if not login_limiter.allow(ip):
        raise HTTPException(429, "尝试过于频繁，请一分钟后重试", headers={"Retry-After": "60"})
    body = await request.json()
    async with password_workers:
        valid = await asyncio.to_thread(verify_credentials, body.get("username"), body.get("password"), secret["account"])
    if not valid:
        raise HTTPException(401, "用户名或密码不正确")
    session = uuid.uuid4().hex
    response = JSONResponse({"authenticated": True})
    response.set_cookie("karpo_demo", issue_session(session, secret["cookie_key"], secret["account"]["username"]),
                        httponly=True, secure=True, samesite="strict", max_age=COOKIE_SECONDS)
    return response


@app.get("/api/catalog")
def get_catalog(request: Request):
    authenticated(request)
    return catalog


@app.get("/api/status")
def status(request: Request):
    session = authenticated(request)
    now = int(time.time())
    groups = {}
    for group, spec in CONFIG["groups"].items():
        value = store.get(group)
        groups[group] = {k: value.get(k) for k in (
            "phase", "stop_after", "error", "ready_gpus", "checked_at", "startup_deadline")}
        groups[group].update(gpu_count=spec["gpu_count"], gpu=spec["gpu"], title=spec["title"],
            session_active=value["sessions"].get(session, 0) > now,
            hosts=[{k: v for k, v in h.items() if k != "address"} for h in value["hosts"]])
    return {"now": now, "idle_seconds": 600, "groups": groups}


@app.post("/api/{group}/start")
def start(group: str, request: Request):
    session = authenticated(request)
    group_name(group)
    value = store.update(group, lambda old: acquire(old, session, int(time.time()), uuid.uuid4().hex))
    return {"phase": value["phase"]}


@app.post("/api/{group}/activity")
def heartbeat(group: str, request: Request):
    session = authenticated(request)
    group_name(group)
    value = store.update(group, lambda old: activity(old, session, int(time.time())))
    return {"stop_after": value["stop_after"]}


@app.post("/api/{group}/end")
def end(group: str, request: Request):
    session = authenticated(request)
    group_name(group)
    value = store.update(group, lambda old: release(old, session, int(time.time())))
    return {"stop_after": value["stop_after"], "phase": value["phase"]}


def validate_input(group, payload):
    if not isinstance(payload, dict) or set(payload) != {"state", "questions"}:
        raise HTTPException(422, "请求必须包含state与questions")
    baseline = catalog[group]["examples"][0]["request"]
    if payload["questions"] != baseline["questions"]:
        raise HTTPException(422, "演示保留原始候选与规则，请仅编辑state中的事实或客户消息")
    if group == "risk":
        try:
            actual = payload["state"]["trusted_features"]
            expected = baseline["state"]["trusted_features"]
            assert isinstance(actual, dict) and all(isinstance(v, dict) for v in actual.values())
            assert set(actual) == set(expected)
            assert all(set(actual[k]) == set(expected[k]) for k in expected)
            assert all(v is None or isinstance(v, (bool, int)) or isinstance(v, float) and math.isfinite(v)
                       for values in actual.values() for v in values.values())
        except (KeyError, TypeError, AssertionError):
            raise HTTPException(422, "请保留完整200个特征及其分组结构") from None
    else:
        try:
            state = json.loads(payload["state"])
            assert isinstance(state, dict) and isinstance(state["trusted_facts"], dict)
            assert isinstance(state["customer_message"], str)
        except (TypeError, KeyError, ValueError, AssertionError):
            raise HTTPException(422, "SOP state应为包含可信事实和客户消息的JSON字符串") from None


@app.post("/api/{group}/infer")
async def infer(group: str, request: Request):
    session = authenticated(request)
    group_name(group)
    payload = await request.json()
    validate_input(group, payload)
    request_id = uuid.uuid4().hex
    value = await asyncio.to_thread(store.update, group, lambda old: begin_request(
        old, session, int(time.time()), request_id))
    begin = time.perf_counter()
    selected = None
    try:
        if value.get("checked_at", 0) < time.time() - 60:
            raise HTTPException(503, "GPU健康状态已过期，正在重新确认")
        targets = []
        for spec in CONFIG["groups"][group]["hosts"]:
            host = next(h for h in value["hosts"] if h["id"] == spec["id"])
            if host["ready"] != len(spec["replicas"]):
                raise HTTPException(503, "GPU尚未全部就绪")
            targets.extend((host["address"], local, replica) for local, replica in enumerate(spec["replicas"]))
        selected = min(targets, key=lambda t: busy.get((group, t[2]), 0))
        key = (group, selected[2])
        busy[key] = busy.get(key, 0) + 1
        response = await app.state.client.post(
            f"https://{selected[0]}:19443/replicas/{selected[1]}/infer", json=payload)
        if response.status_code != 200:
            raise HTTPException(502, "模型服务暂不可用，请稍后重试")
        result = response.json()
        if group == "risk":
            answer = result["answers"]["action"]
            probs = answer["probabilities"]
            score = probs["review"] / (probs["review"] + probs["allow"]) if probs["review"] + probs["allow"] else 0
            threshold = 0.1628260940455841
            raw = answer["choice"]
            action = "review" if raw == "allow" and score >= threshold else raw
            result["decision"] = {"action": action, "raw_action": raw, "review_pair_score": score,
                                  "threshold": threshold, "escalated": action != raw, "policy": "review-threshold-v1"}
        result["demo"] = {"request_id": request_id, "scenario": group, "replica": selected[2],
                          "service_ms": (time.perf_counter() - begin) * 1000, "live_model": True}
        return result
    except httpx.HTTPError:
        raise HTTPException(502, "模型请求失败，请查看GPU就绪状态后重试") from None
    finally:
        if selected:
            busy[(group, selected[2])] -= 1
        def done(old):
            old["requests"].pop(request_id, None)
            return old
        await asyncio.to_thread(store.update, group, done)
