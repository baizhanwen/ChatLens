$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$taskPython = if ($env:CHATLENS_PYTHON) { $env:CHATLENS_PYTHON } else { 'python' }
& $taskPython -m venv .venv-build
& .venv-build/Scripts/python.exe -m pip install --requirement scripts/requirements-build.txt
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .venv-build/Scripts/python.exe -m pip install --requirement services/worker/requirements.txt
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .venv-build/Scripts/python.exe -m PyInstaller --noconfirm --clean --onefile --collect-all tzdata --name chatlens-worker --distpath dist/worker --workpath .build/worker --specpath .build scripts/worker_entry.py
if ($LASTEXITCODE) { exit $LASTEXITCODE }
npm ci --prefix apps/desktop
if ($LASTEXITCODE) { exit $LASTEXITCODE }
npm run pack:win --prefix apps/desktop
exit $LASTEXITCODE
