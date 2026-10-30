#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" = "Darwin" ] && [ ! -x scripts/native/bin/macos-crop ]; then
  if command -v swift >/dev/null 2>&1; then sh scripts/build_native_macos.sh; fi
fi
if [ ! -d apps/desktop/node_modules ]; then npm ci --prefix apps/desktop; fi
exec npm --prefix apps/desktop run dev
