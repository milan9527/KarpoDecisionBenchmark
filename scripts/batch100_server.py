"""Experimental microbatch server; unchanged Karpo checkpoint, one process per GPU.

Only loopback HTTP. Explicit batches and dynamic single-request batches use the
same Decider. No business routing, option filtering, answer cache or retry.
"""
import argparse
from collections import Counter, deque
from concurrent.futures import Future
import json
import multiprocessing as mp
from pathlib import Path
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_model(args, rank):
    import torch
    from karpo_decision import decider as module
    from karpo_decision.serve import qwen_fp8, qwen_graphs
    torch.set_num_threads(4)
    torch.cuda.set_device(rank)
    decider = module.Decider.from_pretrained(args.checkpoint, device=f"cuda:{rank}",
                                            max_length=8192, token_budget=args.token_budget)
    if args.precision == "fp8":
        qwen_fp8.quantize(decider.model)
    if not args.graphs:
        return decider, {}
    workload_lengths = {}
    for scenario in ("risk", "sop"):
        items = json.loads((Path(args.experiment) / f"{scenario}-requests.json").read_text())
        workload_lengths[scenario] = sorted({part.length for item in items for part in decider.plan(item["request"])})
    sizes = sorted({((n + 255) // 256) * 256 for lengths in workload_lengths.values() for n in lengths})
    model = decider.model
    torch._dynamo.config.recompile_limit = 64
    layers = model.layers[:-1] if model.prune_last else model.layers
    eager = [layer.packed_forward for layer in layers]
    compiled = [torch.compile(forward, dynamic=True) for forward in eager]
    for layer, forward in zip(layers, compiled, strict=True):
        layer.packed_forward = forward
    graphs = qwen_graphs.CudaGraphs(model, max(sizes), buckets=((256, 8192),),
                                   segments=args.max_batch, segment_tokens=max(sizes))
    captured = []
    # Bound total padded tokens, not merely number of requests.
    captures = [(size * n, n, size) for size in sizes for n in range(1, args.max_batch + 1)
                if size * n <= args.token_budget]
    for _, n, size in sorted(captures, reverse=True):
        print(json.dumps({"event": "capture", "rank": rank, "batch": n, "tokens_each": size}), flush=True)
        graphs.capture(size, n)
        captured.append([n, size])
    for layer, forward in zip(layers, eager, strict=True):
        layer.packed_forward = forward
    graphs.compiled = compiled
    decider.model = graphs
    return decider, {"captured": captured, "workload_token_lengths": workload_lengths}


def worker(args, rank, incoming, outgoing):
    import torch
    try:
        decider, details = load_model(args, rank)
        outgoing.put({"kind": "ready", "rank": rank, "details": details})
        backlog = deque()
        while True:
            job = backlog.popleft() if backlog else incoming.get()
            if job is None:
                return
            jobs = [job]
            if not job["explicit"]:
                deadline = time.monotonic() + job["wait_ms"] / 1000
                while len(jobs) < job["max_batch"]:
                    try:
                        extra = incoming.get_nowait() if time.monotonic() >= deadline else incoming.get(
                            timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if extra is None:
                        backlog.append(extra)
                        break
                    if extra["explicit"] or extra["config_id"] != job["config_id"]:
                        backlog.append(extra)
                        break
                    jobs.append(extra)
            requests = [request for j in jobs for request in j["requests"]]
            begin = time.perf_counter()
            try:
                plans = [decider.plan(request) for request in requests]
                encoded = time.perf_counter()
                graph_start = getattr(decider.model, "replays", 0)
                torch.cuda.reset_peak_memory_stats(rank)
                responses = decider.decide(requests, plans=plans)
                torch.cuda.synchronize(rank)
                end = time.perf_counter()
                packs = list(decider._packs([(index, part) for index, plan in enumerate(plans) for part in plan]))
                metrics = {
                    "gpu": rank, "batch_size": len(requests), "packs": len(packs),
                    "pack_tokens": [sum(part.length for _, part in group) for group in packs],
                    "graph_replays": getattr(decider.model, "replays", 0) - graph_start,
                    "encode_ms": (encoded - begin) * 1000, "model_ms": (end - encoded) * 1000,
                    "peak_allocated_gib": torch.cuda.max_memory_allocated(rank) / 2**30,
                    "peak_reserved_gib": torch.cuda.max_memory_reserved(rank) / 2**30,
                }
                cursor = 0
                for j in jobs:
                    count = len(j["requests"])
                    outgoing.put({"kind": "result", "id": j["id"], "rank": rank,
                                  "responses": responses[cursor:cursor + count],
                                  "metrics": {**metrics, "queue_ms": (begin - j["enqueued"]) * 1000}})
                    cursor += count
            except Exception:
                error = traceback.format_exc()
                print(error, flush=True)
                for j in jobs:
                    outgoing.put({"kind": "result", "id": j["id"], "rank": rank, "error": error})
    except Exception:
        outgoing.put({"kind": "fatal", "rank": rank, "error": traceback.format_exc()})


class Service:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.pending = {}
        self.busy = [0] * args.replicas
        self.ready = {}
        self.fatal = {}
        self.counter = 0
        self.config = {"replicas": 1, "max_batch": 1, "wait_ms": 0, "config_id": 0}
        ctx = mp.get_context("spawn")
        self.outgoing = ctx.Queue()
        self.incoming = [ctx.Queue(maxsize=512) for _ in range(args.replicas)]
        self.processes = []
        # Sequential GPU initialization avoids simultaneous torch.compile resource spikes.
        threading.Thread(target=self.receive, daemon=True).start()
        for rank, incoming in enumerate(self.incoming):
            p = ctx.Process(target=worker, args=(args, rank, incoming, self.outgoing))
            p.start()
            self.processes.append(p)
            while rank not in self.ready:
                if rank in self.fatal or not p.is_alive():
                    raise RuntimeError(self.fatal.get(rank, f"GPU {rank} exited"))
                time.sleep(.5)

    def receive(self):
        while True:
            result = self.outgoing.get()
            with self.lock:
                if result["kind"] == "ready":
                    self.ready[result["rank"]] = result["details"]
                elif result["kind"] == "fatal":
                    self.fatal[result["rank"]] = result["error"]
                else:
                    future, count = self.pending.pop(result["id"])
                    self.busy[result["rank"]] -= count
                    future.set_result(result)

    def configure(self, body):
        with self.lock:
            if self.pending:
                raise ValueError("Cannot change configuration with requests pending")
            replicas, batch, wait = body["replicas"], body["max_batch"], body["wait_ms"]
            if replicas not in range(1, self.args.replicas + 1) or batch not in [1, 2, 4, 8, 16] or wait not in [0, 2, 5]:
                raise ValueError("Unsupported configuration")
            self.config = dict(replicas=replicas, max_batch=batch, wait_ms=wait,
                               config_id=self.config["config_id"] + 1)
            return dict(self.config)

    def submit(self, requests, explicit):
        future = Future()
        with self.lock:
            if sum(self.busy) + len(requests) > 512:
                raise OverflowError("Admission queue full")
            active = self.config["replicas"]
            rank = min(range(active), key=lambda i: (self.busy[i], (i - self.counter) % active))
            self.counter += 1
            job = dict(self.config, id=self.counter, requests=requests, explicit=explicit, enqueued=time.perf_counter())
            self.pending[job["id"]] = (future, len(requests))
            self.busy[rank] += len(requests)
            self.incoming[rank].put_nowait(job)
        return future.result(timeout=180)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send_json(self, status, body):
        data = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        service = self.server.service
        self.send_json(200, {"ready": service.ready, "fatal": service.fatal,
                             "config": service.config, "busy": service.busy})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024 * 1024:
                self.send_json(413, {"error": "Invalid body size"})
                return
            body = json.loads(self.rfile.read(length))
            if self.path == "/configure":
                self.send_json(200, self.server.service.configure(body))
                return
            explicit = self.path == "/batch"
            if not explicit and self.path != "/v1/systemone":
                self.send_json(404, {"error": "Unknown endpoint"})
                return
            requests = body["requests"] if explicit else [body]
            if not requests or len(requests) > 16:
                raise ValueError("Batch outside 1..16")
            result = self.server.service.submit(requests, explicit)
            if "error" in result:
                self.send_json(500, result)
                return
            result = {"responses": result["responses"], "metrics": result["metrics"]} if explicit else {
                **result["responses"][0], "metrics": result["metrics"]}
            self.send_json(200, result)
        except OverflowError as error:
            self.send_json(429, {"error": str(error)})
        except Exception:
            self.send_json(500, {"error": traceback.format_exc()})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--replicas", type=int, default=2)
    p.add_argument("--max-batch", type=int, default=16)
    p.add_argument("--token-budget", type=int, default=65536)
    p.add_argument("--precision", choices=["bf16", "fp8"], default="fp8")
    p.add_argument("--graphs", action="store_true")
    p.add_argument("--port", type=int, default=18450)
    args = p.parse_args()
    service = Service(args)
    server = Server(("127.0.0.1", args.port), Handler)
    server.service = service
    print(json.dumps({"event": "ready", "replicas": args.replicas}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
