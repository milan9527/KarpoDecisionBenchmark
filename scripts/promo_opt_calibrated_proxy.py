"""Same four-replica router, adding an explicitly separate calibrated decision."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from promo_opt_calibration import decide, load_calibration
from vllm_rps_proxy import make_app


class ReviewPolicy(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path != "/v1/systemone" or response.status_code != 200:
            return response
        begin = time.perf_counter()
        body = b"".join([part async for part in response.body_iterator])
        payload = json.loads(body)
        threshold, digest = request.app.state.calibration
        payload["decision"] = decide(payload["answers"]["action"], threshold)
        payload["decision"]["calibration_sha256"] = digest
        payload["metrics"]["calibration_ms"] = (time.perf_counter() - begin) * 1000
        return JSONResponse(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", nargs="+", required=True)
    parser.add_argument("--port", type=int, default=18770)
    args = parser.parse_args()
    app = make_app(args.urls)
    app.state.calibration = load_calibration()
    app.add_middleware(ReviewPolicy)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
