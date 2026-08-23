#!/usr/bin/env bash
# GTX 10xx (sm_61) needs PyTorch built with CUDA 12.6, not 13.0.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python3}"
PIP="${PIP:-.venv/bin/pip}"

echo ">>> Reinstalling PyTorch cu126 for Pascal GPUs (GTX 1080 Ti / 1060)..."
"${PIP}" install --upgrade torch==2.13.0 torchvision \
  --index-url https://download.pytorch.org/whl/cu126

echo ">>> Verifying CUDA on GPU 0..."
"${PY}" - <<'PY'
import torch
print("torch", torch.__version__)
print("arch list", torch.cuda.get_arch_list())
if not torch.cuda.is_available():
    raise SystemExit("CUDA not available")
x = torch.zeros(1, device="cuda:0")
torch.cuda.synchronize()
print("GPU", torch.cuda.get_device_name(0), "OK", x.device)
PY
echo ">>> Done."
