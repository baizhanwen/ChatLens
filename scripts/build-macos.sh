#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
TASK_PYTHON="${CHATLENS_PYTHON:-python3}"
sh scripts/build_native_macos.sh
"$TASK_PYTHON" -m venv .venv-build
.venv-build/bin/python -m pip install --requirement scripts/requirements-build.txt
.venv-build/bin/python -m pip install --requirement services/worker/requirements.txt
.venv-build/bin/python -m PyInstaller --noconfirm --clean --onefile --name chatlens-worker --distpath dist/worker --workpath .build/worker --specpath .build scripts/worker_entry.py
npm ci --prefix apps/desktop
npm run pack:mac --prefix apps/desktop
