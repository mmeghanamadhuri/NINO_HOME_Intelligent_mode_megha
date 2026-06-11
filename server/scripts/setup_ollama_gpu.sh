#!/usr/bin/env bash
# Install GPU-enabled Ollama on DGX Spark / GB10 (aarch64) and retire the CPU-only snap build.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo bash server/scripts/setup_ollama_gpu.sh"
  exit 1
fi

echo ">>> Stopping snap Ollama (CPU-only on this machine)..."
snap stop ollama.listener 2>/dev/null || true
snap disable ollama 2>/dev/null || true

echo ">>> Installing official Ollama (CUDA / GB10 support)..."
curl -fsSL https://ollama.com/install.sh | sh

echo ">>> Configuring Ollama for GPU inference on GB10..."
mkdir -p /etc/systemd/system/ollama.service.d
cat >/etc/systemd/system/ollama.service.d/gpu.conf <<'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="CUDA_VISIBLE_DEVICES=0"
EOF

systemctl daemon-reload
systemctl enable ollama
systemctl restart ollama

echo ">>> Waiting for Ollama API..."
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b}"
echo ">>> Warming model: ${MODEL}"
ollama pull "${MODEL}" || true
ollama run "${MODEL}" "ready" >/dev/null || true

echo ">>> Runtime status:"
ollama ps || true
PROC="$(ollama ps 2>/dev/null | awk 'NR==2 {print $4}')"
VRAM="$(curl -sf http://127.0.0.1:11434/api/ps | python3 -c "import sys,json; m=json.load(sys.stdin).get('models',[{}])[0]; print(m.get('size_vram',0))" 2>/dev/null || echo 0)"

if [[ "${PROC}" == *"GPU"* && "${VRAM}" != "0" ]]; then
  echo "OK: Ollama is using the GPU (${PROC}, size_vram=${VRAM})."
else
  echo "WARNING: Ollama may still be on CPU (processor=${PROC:-unknown}, size_vram=${VRAM})."
  echo "  - Ensure no other large models are loaded: ollama ps"
  echo "  - Unload and reload: ollama stop ${MODEL} && ollama run ${MODEL} hi"
  echo "  - Free GPU memory from stopped Docker containers if any were used recently."
fi

echo ">>> Done. Restart the NiNO server and check /api/status -> llm.on_gpu"
