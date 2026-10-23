$ErrorActionPreference = 'Stop'
$labRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $labRoot
$labTemp = Join-Path $labRoot '.runtime/tmp'
New-Item -ItemType Directory -Path $labTemp -Force | Out-Null
$env:TEMP = $labTemp
$env:TMP = $labTemp
$labPython = Join-Path $labRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $labPython)) {
    & python -m venv '.venv'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Lab virtual environment.' }
}
& $labPython -X utf8 -m ensurepip --upgrade --default-pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to prepare pip.' }
$labRequirements = if (Test-Path -LiteralPath 'backend/requirements.lock.txt') { 'backend/requirements.lock.txt' } else { 'backend/requirements.in' }
& $labPython -X utf8 -m pip --isolated install --index-url 'https://pypi.org/simple' --cache-dir '.runtime/pip-cache' -r $labRequirements --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Failed to install Python dependencies.' }
Push-Location -LiteralPath 'frontend'
try {
    if (Test-Path -LiteralPath 'package-lock.json') {
        & npm.cmd ci --registry 'https://registry.npmjs.org' --cache '../.runtime/npm-cache' --no-fund --no-audit
    } else {
        & npm.cmd install --registry 'https://registry.npmjs.org' --cache '../.runtime/npm-cache' --no-fund --no-audit
    }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install frontend dependencies.' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Write-Output 'Lab environment is ready. Run the shared-watchlist launcher next.'
