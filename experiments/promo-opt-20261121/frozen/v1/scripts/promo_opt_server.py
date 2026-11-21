"""Karpo vLLM replica for fixed-arrival-rate tests; original input is preserved."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import dataclasses
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
import uuid

os.environ.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
os.environ.setdefault("VLLM_PLUGINS", "karpo_benchmark")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ["PATH"] = str(Path(sys.executable).parent) + ":" + os.environ.get("PATH", "")
if Path("/usr/local/cuda-13.0/bin/nvcc").exists():
    os.environ.setdefault("CUDA_HOME", "/usr/local/cuda-13.0")
    os.environ["PATH"] = "/usr/local/cuda-13.0/bin:" + os.environ["PATH"]

from vllm_karpo_encoding import (
    encode_request,
    load_encoder,
    response_from_logits,
    token_digest,
)


def graph_config(lengths, max_seqs, policy):
    batches = range(1, max_seqs + 1) if policy == "dense" else (1, 2, 4, 8, 16)
    token_budget = min(65536, 8192 * max_seqs)
    captures = sorted({length * batch for length in lengths for batch in batches
                       if batch <= max_seqs and length * batch <= token_budget})
    return token_budget, captures


def main(args):
    import torch
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from vllm import AsyncEngineArgs, PoolingParams
    from vllm.v1.engine.async_llm import AsyncLLM

    torch.set_num_threads(4)
    encoder, config = load_encoder(args.checkpoint)
    engine = None
    inflight = 0
    maximum_inflight = 0
    encode_lock = asyncio.Lock()
    lengths = set()
    for scenario in ("risk", "sop"):
        for item in json.loads((Path(args.experiment) / f"{scenario}-requests.json").read_text()):
            _, ids, _ = encode_request(encoder, item["request"])
            lengths.add(((len(ids) + 255) // 256) * 256)
    token_budget, capture_sizes = graph_config(lengths, args.max_seqs, args.graph)
    settings = {
        "model": args.checkpoint,
        "runner": "pooling",
        "convert": "none",
        "dtype": "bfloat16",
        "quantization": "fp8_per_channel",
        "quantization_config": {
            "ignore": ["*.in_proj_ba", "*.in_proj_a", "*.in_proj_b", "visual.*", "*.visual.*", "score"]
        },
        "hf_overrides": {"architectures": ["KarpoForSequenceClassification"]},
        "skip_tokenizer_init": True,
        "language_model_only": True,
        "max_model_len": 8192,
        "max_num_batched_tokens": token_budget,
        "max_num_seqs": args.max_seqs,
        "enable_prefix_caching": False,
        "enable_chunked_prefill": False,
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "disable_log_stats": False,
        "enable_log_requests": False,
        "tensor_parallel_size": 1,
        "worker_extension_cls": "karpo_vllm_worker.KarpoInspectionWorker",
        "seed": 0,
        "compilation_config": {"prefill_cudagraph_capture_token_sizes": capture_sizes},
    }
    if args.eager:
        settings["enforce_eager"] = True
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal engine
        engine_args = AsyncEngineArgs(**settings)
        engine = AsyncLLM.from_engine_args(engine_args)
        runtime = engine.vllm_config
        if runtime.model_config.hf_text_config.is_causal:
            raise RuntimeError("Causal full attention would change Karpo semantics")
        if runtime.cache_config.enable_prefix_caching or runtime.scheduler_config.enable_chunked_prefill:
            raise RuntimeError("Karpo requires complete uncached bidirectional input")
        info = {
            "settings": settings,
            "vllm": importlib.metadata.version("vllm"),
            "torch": torch.__version__,
            "full_attention_is_causal": runtime.model_config.hf_text_config.is_causal,
            "head_dtype": str(runtime.model_config.head_dtype),
            "compilation": dataclasses.asdict(runtime.compilation_config),
            "attention": str(runtime.attention_config),
            "quantization": str(runtime.quant_config),
        }
        info["workers"] = await engine.collective_rpc("inspect_karpo")
        from safetensors.torch import load_file
        import hashlib
        original = load_file(str(Path(args.checkpoint) / "readout.safetensors"))["weight"]
        original_hash = hashlib.sha256(original.float().contiguous().numpy().tobytes()).hexdigest()
        for worker in info["workers"]:
            if worker["readout_sha256"] != original_hash or worker["readout_dtype"] != "torch.float32":
                raise RuntimeError("The original FP32 readout was not loaded exactly")
            if not any("float8" in dtype for dtype in worker["parameter_elements_by_dtype"]):
                raise RuntimeError("The worker did not load FP8 projection weights")
            expected_attention_layers = runtime.model_config.hf_text_config.layer_types.count("full_attention")
            if len(worker["attention"]) != expected_attention_layers or any(
                "ENCODER_ONLY" not in item["type"].upper() for item in worker["attention"]
            ):
                raise RuntimeError(f"Unexpected full-attention types: {worker['attention']}")
        (output / "engine-config.json").write_text(json.dumps(info, default=str, indent=2) + "\n")
        try:
            yield
        finally:
            engine.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def health():
        return {
            "backend": "vllm",
            "max_num_seqs": args.max_seqs,
            "inflight": inflight,
            "maximum_inflight": maximum_inflight,
            "graph_policy": args.graph,
            "admission_limit": args.admission_limit,
            "replica_id": args.replica_id,
            "settings": settings,
        }

    @app.post("/v1/systemone")
    async def decide(request: Request):
        nonlocal inflight, maximum_inflight
        begin = time.perf_counter()
        if inflight >= args.admission_limit:
            raise HTTPException(429, "Admission queue full")
        inflight += 1
        maximum_inflight = max(maximum_inflight, inflight)
        try:
            body = await request.json()
            # One CPU encoder worker overlaps with the independent GPU engine.
            # The lock preserves the processor's thread safety.
            async with encode_lock:
                encode_start = time.perf_counter()
                part, ids, marker = await asyncio.to_thread(encode_request, encoder, body)
            encoded = time.perf_counter()
            params = PoolingParams(
                task="classify", use_activation=False, extra_kwargs={"karpo_marker": marker}
            )
            result = None
            async for item in engine.encode(
                {"prompt_token_ids": ids}, params, request_id=uuid.uuid4().hex
            ):
                result = item
            if result is None or not result.finished:
                raise RuntimeError("vLLM did not finish the request")
            logits = result.outputs.data.tolist()
            response = response_from_logits(body, logits, config, len(ids))
            response["metrics"] = {
                "backend": "vllm",
                "replica_id": args.replica_id,
                "gpu": 0,
                "max_num_seqs": args.max_seqs,
                "encode_queue_ms": (encode_start - begin) * 1000,
                "encode_ms": (encoded - encode_start) * 1000,
                "engine_wait_and_model_ms": (time.perf_counter() - encoded) * 1000,
                "tokens_sha256": token_digest(ids),
                "marker": marker,
            }
            return response
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error
        finally:
            inflight -= 1

    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--experiment", default=str(Path(__file__).resolve().parents[1] /
                                                   "experiments/vllm-rps100-20261120"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-seqs", type=int, choices=[1, 2, 4, 8, 16], required=True)
    parser.add_argument("--graph", choices=["powers", "dense"], default="powers")
    parser.add_argument("--replica-id", type=int, default=0)
    parser.add_argument("--admission-limit", type=int, default=64)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.8)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18450)
    parser.add_argument("--eager", action="store_true")
    main(parser.parse_args())
