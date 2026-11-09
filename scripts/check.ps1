param([switch]$Browser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force test-results | Out-Null
& .\.runtime\python\python.exe -m ruff check flowdesk tests scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.runtime\python\python.exe -m pytest --junitxml=test-results\pytest.xml
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Browser) {
    & .\.runtime\python\python.exe scripts\browser_qa.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
