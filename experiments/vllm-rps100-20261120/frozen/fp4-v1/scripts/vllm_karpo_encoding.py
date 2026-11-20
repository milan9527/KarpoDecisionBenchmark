"""The same pinned Karpo encoding and answer construction as the native path."""

import hashlib
import json
from pathlib import Path


def token_digest(ids):
    # Portable representation independent of CPU byte order and torch version.
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


def load_encoder(checkpoint):
    from karpo_decision.checkpoint import load_processor
    from karpo_decision.config import DecisionConfig
    from karpo_decision.encoding import Encoder
    from karpo_decision.templates import template

    config = DecisionConfig.load(checkpoint)
    processor = load_processor(checkpoint, config.max_pixels)
    return (
        Encoder(processor, config.codes, template(config.template), config.marker),
        config,
    )


def encode_request(encoder, request):
    # This adapter is deliberately scoped to the two text-only, single-question
    # fixtures. Do not silently approximate other Karpo request contracts.
    if len(request.get("questions", {})) != 1:
        raise ValueError("This benchmark adapter requires exactly one question")
    parts = encoder.plan(request, max_length=8192)
    if len(parts) != 1:
        raise ValueError("This benchmark adapter requires one complete sequence")
    part = parts[0]
    if part.pixel_values is not None or part.images or part.positions is not None:
        raise ValueError("This benchmark adapter supports plain text only")
    if len(part.markers) != 1:
        raise ValueError("Expected one original Karpo marker")
    ids = part.input_ids.tolist()
    marker = int(part.markers[0])
    if ids[marker] != encoder.marker_id:
        raise ValueError("Marker token mismatch")
    return part, ids, marker


def response_from_logits(request, logits, config, length):
    import torch
    from karpo_decision.answers import answer
    from karpo_decision.protocol import options

    key, question = next(iter(request["questions"].items()))
    count = len(options(question)[0])
    if len(logits) != len(config.codes):
        raise ValueError("Readout has the wrong number of answer codes")
    values = torch.as_tensor(logits, dtype=torch.float32)[:count]
    if not torch.isfinite(values).all():
        raise ValueError("Nonfinite readout")
    probabilities = (values / config.temperatures[question["type"]]).softmax(-1).tolist()
    return {
        "answers": {key: answer(question, probabilities)},
        "usage": {"input_tokens": length, "output_tokens": 0, "sequences": 1},
    }


def encoding_manifest(checkpoint, experiment):
    encoder, _ = load_encoder(checkpoint)
    rows = []
    for scenario in ("risk", "sop"):
        for item in json.loads((Path(experiment) / f"{scenario}-requests.json").read_text()):
            part, ids, marker = encode_request(encoder, item["request"])
            rows.append(
                {
                    "request_id": item["id"],
                    "request_sha256": item["request_sha256"],
                    "tokens_sha256": token_digest(ids),
                    "length": len(ids),
                    "marker": marker,
                    "trailing_tokens": ids[marker + 1 :],
                    "keys": list(part.keys),
                    "counts": list(part.counts),
                }
            )
    return rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    Path(args.output).write_text(
        json.dumps(encoding_manifest(args.checkpoint, args.experiment), indent=2) + "\n"
    )
