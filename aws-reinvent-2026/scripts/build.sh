#!/usr/bin/env bash
# Package the three Lambda functions into build/*.zip (pure Python deps, so it
# builds the same on macOS and Linux).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build && mkdir -p build
for fn in workflow web notifier; do
  dir=build/$fn; mkdir -p "$dir"
  find functions/$fn -maxdepth 1 -type f -exec cp {} "$dir"/ \;
  if [ -f functions/$fn/requirements.txt ]; then
    python3 -m pip install -q --no-warn-conflicts --disable-pip-version-check --target "$dir" -r functions/$fn/requirements.txt
  fi
  rm -f "$dir/requirements.txt"
  (cd "$dir" && zip -qr "../$fn.zip" . -x '*__pycache__*')
  echo "built build/$fn.zip"
done
