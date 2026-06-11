#!/usr/bin/env bash
# User-local GPU Ollama for DGX Spark GB10 (no sudo). Models reuse ~/.ollama/models.
set -euo pipefail

INSTALL_DIR="${OLLAMA_GPU_HOME:-$HOME/.local/ollama-gpu}"
ARCH="arm64"
VERSION_URL="https://ollama.com/download/ollama-linux-${ARCH}.tar.zst"
TMP_ARCHIVE="$(mktemp /tmp/ollama-linux-${ARCH}.XXXXXX.tar.zst)"

cleanup() { rm -f "${TMP_ARCHIVE}"; }
trap cleanup EXIT

if [[ -x "${INSTALL_DIR}/bin/ollama" ]]; then
  echo "GPU Ollama already installed at ${INSTALL_DIR}"
  "${INSTALL_DIR}/bin/ollama" --version
  exit 0
fi

command -v zstd >/dev/null || { echo "Install zstd: sudo apt install zstd"; exit 1; }
command -v curl >/dev/null || { echo "curl is required"; exit 1; }

echo ">>> Downloading official Ollama (${ARCH}, CUDA)..."
echo "    This is ~1.5 GB and may take several minutes."
mkdir -p "${INSTALL_DIR}"
curl -fL --progress-bar "${VERSION_URL}" -o "${TMP_ARCHIVE}"
echo ">>> Extracting to ${INSTALL_DIR}..."
zstd -d "${TMP_ARCHIVE}" --stdout | tar -xf - -C "${INSTALL_DIR}"
chmod +x "${INSTALL_DIR}/bin/ollama"
echo ">>> Installed:"
"${INSTALL_DIR}/bin/ollama" --version
