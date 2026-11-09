param([ValidateSet('demo','telegram')][string]$Mode = 'demo', [int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Run powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 first.'
}
& $pythonPath -m flowdesk $Mode --port $Port
exit $LASTEXITCODE
