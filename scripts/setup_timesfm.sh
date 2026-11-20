#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

environment="$PWD/.venv-tsfm"
checkpoint="$environment/checkpoint"
ledger="$HOME/.claude/tools/heavy_install.sh"
checkpoint_url='https://huggingface.co/google/timesfm-2.5-200m-pytorch/resolve/1d952420fba87f3c6dee4f240de0f1a0fbc790e3'
weights_hash='2f776efe6245e42b24bc4153ffdf61810140210e4bd3b01fb21f7aa779ab6ce8'

if [[ -f "$ledger" ]]; then
    bash "$ledger" add "$environment" 'decision-engine TimesFM 2.5 Python environment and checkpoint'
fi
"${TIMESFM_PYTHON:-/usr/local/bin/python3}" -m venv "$environment"
"$environment/bin/python" -m pip install --no-cache-dir 'timesfm[torch]==2.0.2'
mkdir -p "$checkpoint"

check_weights() {
    printf '%s  %s\n' "$weights_hash" "$1" | shasum -a 256 --check --status
}

if [[ ! -s "$checkpoint/config.json" ]]; then
    curl -fLsS --retry 2 "$checkpoint_url/config.json" -o "$checkpoint/config.json.part"
    mv "$checkpoint/config.json.part" "$checkpoint/config.json"
fi
if ! check_weights "$checkpoint/model.safetensors" 2>/dev/null; then
    curl -fLsS --retry 2 "$checkpoint_url/model.safetensors" -o "$checkpoint/model.safetensors.part"
    check_weights "$checkpoint/model.safetensors.part"
    mv "$checkpoint/model.safetensors.part" "$checkpoint/model.safetensors"
fi

if [[ -f "$ledger" ]]; then
    bash "$ledger" adopt "$environment"
fi
