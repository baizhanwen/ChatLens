$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (!(Test-Path 'apps/desktop/node_modules')) { npm ci --prefix apps/desktop; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
$env:CHATLENS_PYTHON = if ($env:CHATLENS_PYTHON) { $env:CHATLENS_PYTHON } else { 'python' }
& $env:CHATLENS_PYTHON -m pip install --requirement services/worker/requirements.txt
if ($LASTEXITCODE) { exit $LASTEXITCODE }
npm --prefix apps/desktop run dev
