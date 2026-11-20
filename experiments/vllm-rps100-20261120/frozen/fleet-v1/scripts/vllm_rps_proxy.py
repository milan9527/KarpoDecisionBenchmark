"""Asynchronous least-inflight routing; a replica always receives original JSON."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn


def make_app(urls):
    busy = [0] * len(urls)
    active = list(range(len(urls)))
    cursor = 0

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=120, trust_env=False,
            limits=httpx.Limits(max_connections=2048, max_keepalive_connections=2048),
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def health():
        result = await asyncio.gather(*(
            app.state.client.get(urls[i] + "/healthz") for i in active
        ))
        for response in result:
            response.raise_for_status()
        return {"active": active, "busy": busy, "backends": [r.json() for r in result]}

    @app.post("/configure")
    async def configure(request: Request):
        nonlocal active
        config = await request.json()
        selected = config.get("active", list(range(config.get("replicas", len(urls)))))
        if not selected or len(set(selected)) != len(selected) or any(
            isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(urls) for i in selected
        ):
            raise HTTPException(400, "Invalid replica indices")
        if any(busy):
            raise HTTPException(409, "Requests are still pending")
        active = selected
        return {"active": active}

    @app.post("/v1/systemone")
    async def decide(request: Request):
        nonlocal cursor
        begin = time.perf_counter()
        body = await request.json()
        index = min(active, key=lambda i: (busy[i], (i - cursor) % len(urls)))
        busy[index] += 1
        cursor = (index + 1) % len(urls)
        try:
            response = await app.state.client.post(urls[index] + "/v1/systemone", json=body)
            payload = response.json()
            if response.is_success:
                payload["metrics"]["proxy_replica"] = index
                payload["metrics"]["proxy_ms"] = (time.perf_counter() - begin) * 1000
            return JSONResponse(payload, status_code=response.status_code)
        except httpx.HTTPError as error:
            return JSONResponse({"error": str(error)}, status_code=502)
        finally:
            busy[index] -= 1

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", nargs="+", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18450)
    args = parser.parse_args()
    uvicorn.run(make_app(args.urls), host=args.host, port=args.port, access_log=False)
