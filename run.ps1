$ErrorActionPreference = 'Stop'
try {
    if ($env:REALTIME_SUBTITLE_GUI_LAUNCH -eq '1') {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    }
    Set-Location -LiteralPath $PSScriptRoot
    $env:PYTHONUTF8 = '1'
    . (Join-Path $PSScriptRoot 'launcher.ps1')
    $taskPython = Get-SubtitlePython -ProjectRoot $PSScriptRoot
    if ($env:REALTIME_SUBTITLE_GUI_LAUNCH -eq '1') {
        [Console]::WriteLine('REALTIME_SUBTITLE_RUNTIME_READY')
        $taskStart = New-Object System.Diagnostics.ProcessStartInfo
        $taskStart.FileName = $taskPython
        $taskStart.Arguments = $env:REALTIME_SUBTITLE_PYTHON_ARGS
        $taskStart.WorkingDirectory = $PSScriptRoot
        $taskStart.UseShellExecute = $false
        $taskStart.CreateNoWindow = $true
        $taskStart.RedirectStandardOutput = $true
        $taskStart.RedirectStandardError = $true
        $taskStart.EnvironmentVariables.Remove('REALTIME_SUBTITLE_PYTHON_ARGS')
        $taskChild = [System.Diagnostics.Process]::Start($taskStart)
        try {
            $taskOutput = $taskChild.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
            $taskError = $taskChild.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
            $taskChild.WaitForExit()
            [Threading.Tasks.Task]::WaitAll(@($taskOutput, $taskError), 1000) | Out-Null
            exit $taskChild.ExitCode
        } finally {
            $taskChild.Dispose()
        }
    }
    & $taskPython (Join-Path $PSScriptRoot 'main.py') @args
    exit $LASTEXITCODE
} catch {
    Write-Host ('Startup failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
