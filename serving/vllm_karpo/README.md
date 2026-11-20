# Karpo 4B / vLLM 0.34.0 benchmark adapter

This adapter runs the original Karpo decision checkpoint with vLLM. It is scoped
to this repository's single-question, text-only risk and SOP workloads.

The backbone weights, tokenizer, input template, option order and readout weights
come from the pinned Karpo checkpoint. The API uses Karpo's own `Encoder` and
answer construction. It submits every input token to vLLM, including the nine
chat-template tokens following the answer marker.

The registered `KarpoForSequenceClassification` class reuses vLLM's Qwen3.5/D3
weight loader. It adds two required adaptations:

- Interpret Karpo's `attention: bidirectional` field. All eight full-attention
  operators run as `encoder_only`; the GatedDeltaNet layers remain causal.
- Select the hidden state at the original Karpo marker. The FP32 readout scores
  that position, rather than the final token of the prompt.

vLLM's configuration log still names the sequence-pooling default `LAST`.
The model's `_init_pooler` installs `MarkerPool` as the actual pooling operator;
its ragged-sequence test explicitly distinguishes marker positions from final
token positions.

The readout is excluded from FP8 quantization. Decoder projections use
`fp8_per_channel`, and the small GatedDeltaNet gates remain unquantized.
Every startup checks the loaded output-head hash, FP8 parameter presence and
actual attention operators through a named worker RPC.

Prefix caching and chunked prefill are disabled. CUDA Graph capture sizes are
derived from the original workload lengths. HTTP concurrency is independent of
`max_num_seqs`, which limits the GPU scheduler's active sequences.

## Run

Use a separate virtual environment with vLLM 0.34.0, Transformers 5.21.0 and
Karpo revision `6600011eb5cf46232d056ec0717dc726146d29de`.
The measured environment and complete dependencies are saved with the experiment.

```bash
python -m pip install ./serving/vllm_karpo
python scripts/vllm_karpo_server.py \
  --checkpoint /opt/karpo/models/karpo-4b \
  --experiment experiments/vllm-single-20261120 \
  --output /tmp/karpo-vllm-run \
  --max-seqs 4
```

The process exposes `GET /healthz` and `POST /v1/systemone` on
`127.0.0.1:18450`. Requests use the original Karpo JSON contract. Inputs outside
the single-question text-only scope, or over 8,192 tokens, fail explicitly.

Tests in the same environment:

```bash
python tests/test_vllm_karpo.py
```

The full comparison uses the frozen manifest, original fixtures, native FP8
baseline, quality checks, sustained C100 tests and independent confirmations in
`scripts/vllm_single_suite.py`. That script assumes the retained benchmark host's
two virtual environments and private result bucket; it is an experiment runner,
not a general deployment command.

See [vLLM's D3 implementation](https://github.com/vllm-project/vllm/blob/v0.34.0/vllm/model_executor/models/d3.py)
and [Karpo's template](https://github.com/machinepulse-ai/karpo-decision/blob/6600011eb5cf46232d056ec0717dc726146d29de/src/karpo_decision/templates.py)
for the underlying contracts.
