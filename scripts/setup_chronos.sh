#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

environment="$PWD/.venv-chronos"
checkpoint="$environment/checkpoint"
ledger="$HOME/.claude/tools/heavy_install.sh"
checkpoint_url='https://huggingface.co/amazon/chronos-bolt-tiny/resolve/a0e552de83495b5c28c14c71c374f3e33280b340'
weights_hash='75068728d376d2bec670379eeef4bfb4d24c0cfe24d957451f8d19b447030a32'

if [[ -f "$ledger" ]]; then
    bash "$ledger" add "$environment" 'decision-engine Chronos-Bolt Tiny environment and checkpoint'
fi
"${CHRONOS_PYTHON:-/usr/local/bin/python3}" -m venv "$environment"
"$environment/bin/python" -m pip install --no-cache-dir 'chronos-forecasting==2.2.2'     'torch==2.10.0' 'transformers==4.57.6' 'numpy==2.3.5' 'scikit-learn==1.7.2'
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
