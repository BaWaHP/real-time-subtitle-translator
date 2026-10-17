$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
. (Join-Path $PSScriptRoot 'launcher.ps1')
try {
    $taskPython = Get-SubtitlePython -ProjectRoot $PSScriptRoot
    & $taskPython (Join-Path $PSScriptRoot 'main.py') @args
    exit $LASTEXITCODE
} catch {
    Write-Host ('Startup failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
