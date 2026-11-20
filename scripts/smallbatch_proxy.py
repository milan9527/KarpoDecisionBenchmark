"""Identical extra HTTP hop for every measurement; four replicas use two hosts."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


class Router:
    def __init__(self, urls):
        self.urls = urls
        self.clients = [httpx.Client(base_url=url, timeout=180, trust_env=False,
                                    limits=httpx.Limits(max_connections=256, max_keepalive_connections=128))
                        for url in urls]
        self.lock = threading.Lock()
        self.busy = [0] * len(urls)
        self.active = 1
        self.counter = 0
        self.config = {"replicas": 1, "max_batch": 1, "wait_ms": 0}

    def health(self):
        backends = []
        for url, client in zip(self.urls, self.clients, strict=True):
            response = client.get("/healthz")
            response.raise_for_status()
            backends.append({"url": url, "health": response.json()})
        return {"backends": backends, "config": self.config, "busy": self.busy}

    def configure(self, config):
        replicas = config["replicas"]
        if replicas not in (1, 2, 4) or replicas > len(self.clients) * 2:
            raise ValueError("Unsupported replica count")
        with self.lock:
            if any(self.busy):
                raise ValueError("Requests are pending")
            for index, client in enumerate(self.clients):
                backend = dict(config, replicas=min(replicas, 2))
                response = client.post("/configure", json=backend)
                response.raise_for_status()
            self.active = 2 if replicas == 4 else 1
            self.config = dict(config)
        return self.config

    def call(self, request):
        with self.lock:
            index = min(range(self.active),
                        key=lambda i: (self.busy[i], (i - self.counter) % self.active))
            self.busy[index] += 1
            self.counter += 1
        try:
            response = self.clients[index].post("/v1/systemone", json=request)
            body = response.json()
            if response.is_success:
                body["metrics"]["replica_host"] = index
                body["metrics"]["global_gpu"] = index * 2 + body["metrics"]["gpu"]
            return response.status_code, body
        finally:
            with self.lock:
                self.busy[index] -= 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send(self, status, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.send(200, self.server.router.health())
        except Exception as error:
            self.send(503, {"error": str(error)})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16 * 1024 * 1024:
                self.send(413, {"error": "Invalid request size"})
                return
            body = json.loads(self.rfile.read(size))
            if self.path == "/configure":
                self.send(200, self.server.router.configure(body))
            elif self.path == "/v1/systemone":
                status, response = self.server.router.call(body)
                self.send(status, response)
            else:
                self.send(404, {"error": "Unknown endpoint"})
        except Exception as error:
            self.send(500, {"error": f"{type(error).__name__}: {error}"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backends", nargs="+", required=True)
    parser.add_argument("--port", type=int, default=18451)
    args = parser.parse_args()
    router = Router(args.backends)
    router.health()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.request_queue_size = 512
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.router = router
    server.serve_forever()


if __name__ == "__main__":
    main()
