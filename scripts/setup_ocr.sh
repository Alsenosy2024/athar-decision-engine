#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

directory="$PWD/.ocr-data"
ledger="$HOME/.claude/tools/heavy_install.sh"
source_url='https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0'

if [[ -f "$ledger" ]]; then
    bash "$ledger" add "$directory" 'decision-engine offline Tesseract Arabic and English language models'
fi
mkdir -p "$directory"

download() {
    local language="$1" hash="$2" file
    file="$directory/$language.traineddata"
    if ! printf '%s  %s\n' "$hash" "$file" | shasum -a 256 --check --status 2>/dev/null; then
        curl -fLsS --retry 2 "$source_url/$language.traineddata" -o "$file.part"
        printf '%s  %s\n' "$hash" "$file.part" | shasum -a 256 --check --status
        mv "$file.part" "$file"
    fi
}

download ara e3206d3dc87fd50c24a0fb9f01838615911d25168f4e64415244b67d2bb3e729
download eng 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2

if [[ -f "$ledger" ]]; then
    bash "$ledger" adopt "$directory"
fi
