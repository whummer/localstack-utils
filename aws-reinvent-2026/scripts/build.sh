#!/usr/bin/env bash
# Package the functions into build/*.zip. workflow, notifier and sandbox are Python (pure
# Python deps, so they build the same on macOS and Linux); web is a Node.js app.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build && mkdir -p build
for fn in workflow notifier sandbox web; do
  dir=build/$fn; mkdir -p "$dir"
  find functions/$fn -maxdepth 1 -type f -exec cp {} "$dir"/ \;
  # planner.py is shared by the workflow (guardrail, journal drafting, fallback planner)
  if [ "$fn" = workflow ]; then cp functions/common/planner.py "$dir"/; fi
  if [ -f functions/$fn/requirements.txt ]; then
    python3 -m pip install -q --no-warn-conflicts --disable-pip-version-check --target "$dir" -r functions/$fn/requirements.txt
    rm -f "$dir/requirements.txt"
  fi
  if [ -f functions/$fn/package.json ]; then
    (cd "$dir" && npm ci --omit=dev --silent)
  fi
  (cd "$dir" && zip -qr "../$fn.zip" . -x '*__pycache__*')
  echo "built build/$fn.zip"
done
