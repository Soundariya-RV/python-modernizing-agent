#!/usr/bin/env bash
# Serve Qwen2.5-Coder-32B-Instruct (Q4_K_M GGUF) with llama.cpp on a small GPU
# (here: one RTX 2080 Ti, 11 GB). Layers that do not fit in VRAM stay in RAM.
# Exposes the same OpenAI-compatible API as vLLM on http://localhost:8000/v1.
#
#   ./serve_qwen_llamacpp.sh            # foreground
#   MODEL=/path/to.gguf PORT=8001 ./serve_qwen_llamacpp.sh
set -euo pipefail
LLAMA_SERVER=${LLAMA_SERVER:-$HOME/envs/llamacpp/bin/llama-server}
MODEL=${MODEL:-$HOME/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf}
PORT=${PORT:-8000}
CTX=${CTX:-32768}

# --jinja: use the model's chat template, which carries Qwen's Hermes tool calls
# -np 1: one request at a time, so the whole context goes to the agent
# q8_0 KV cache: halves KV memory so more layers fit on the GPU
exec "$LLAMA_SERVER" -m "$MODEL" --alias Qwen/Qwen2.5-Coder-32B-Instruct \
    --host 127.0.0.1 --port "$PORT" --device CUDA0 \
    -c "$CTX" -np 1 --jinja -fa on -ctk q8_0 -ctv q8_0 \
    --fit on -t "$(nproc)"
