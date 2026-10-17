$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
. (Join-Path $PSScriptRoot 'launcher.ps1')
try {
    $taskPython = Get-SubtitlePython -ProjectRoot $PSScriptRoot
    Write-Host ('Ready: ' + $taskPython)
    exit 0
} catch {
    Write-Host ('Setup failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
