param([switch]$Dev, [switch]$Telegram)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    New-Item -ItemType Directory -Force .runtime | Out-Null
    Write-Host 'Downloading isolated Python 3.13.12 from python.org...'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.13.12/python-3.13.12-embed-amd64.zip' -OutFile '.runtime\python.zip'
    Expand-Archive -LiteralPath '.runtime\python.zip' -DestinationPath '.runtime\python' -Force
    Set-Content -LiteralPath '.runtime\python\python313._pth' -Encoding ASCII -Value "python313.zip`n.`n..\..`nimport site"
}
if (-not (Test-Path -LiteralPath '.runtime\python\Lib\site-packages\pip\__main__.py')) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '.runtime\get-pip.py'
    & $pythonPath '.runtime\get-pip.py' --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw 'pip installation failed.' }
}
$requirements = 'requirements.txt'
if (Test-Path -LiteralPath 'requirements-lock.txt') { $requirements = 'requirements-lock.txt' }
& $pythonPath -m pip install -r $requirements --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($Dev) {
    & $pythonPath -m pip install -r requirements-dev-lock.txt --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw 'Test dependency installation failed.' }
}
if ($Telegram) {
    & $pythonPath -m pip install -r requirements-telegram-lock.txt --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw 'Telegram dependency installation failed.' }
}
& $pythonPath scripts\init_config.py
if ($LASTEXITCODE -ne 0) { throw 'Configuration failed. Check .env.' }
Write-Host 'Run: powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1'
