# Shared by run.ps1 and setup.ps1. Keep this file ASCII for Windows PowerShell 5.1.
Set-StrictMode -Version 2.0

function Test-SubtitlePython {
    param([Parameter(Mandatory = $true)][string]$Python)
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
    try {
        & $Python -I -c 'import sys; assert sys.version_info[:2] == (3, 11); import PySide6.QtWidgets, faster_whisper, pyaudiowpatch, soxr, httpx, jwt, keyring, filelock, yt_dlp, imageio_ffmpeg' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Get-SubtitleUv {
    $taskUvCommand = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($taskUvCommand) { return $taskUvCommand.Source }
    $taskUserUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $taskUserUv -PathType Leaf) { return $taskUserUv }
    throw 'uv was not found. Install uv first: winget install --id astral-sh.uv --exact'
}

function Invoke-SubtitleUv {
    param([string]$Uv, [string[]]$Arguments)
    & $Uv @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw ('Runtime setup failed (uv exit ' + $LASTEXITCODE + '). Check the message above, then run setup.bat again.')
    }
}

function Get-SubtitlePython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $taskRuntimeRoot = Join-Path $ProjectRoot '.runtime'
    $taskInstallDir = Join-Path $taskRuntimeRoot 'python'
    $taskEnvironment = Join-Path $taskRuntimeRoot 'venv'
    $taskPython = Join-Path $taskEnvironment 'Scripts\python.exe'
    if (Test-SubtitlePython -Python $taskPython) { return $taskPython }

    if (Test-Path -LiteralPath $taskEnvironment) {
        $taskInUse = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($taskEnvironment + '\', [StringComparison]::OrdinalIgnoreCase)) -or
                ($_.CommandLine -and $_.CommandLine.IndexOf($taskEnvironment, [StringComparison]::OrdinalIgnoreCase) -ge 0)
            }
        if ($taskInUse) { throw 'The subtitle runtime is still in use. Close the subtitle app, then run setup.bat.' }
    }

    Write-Host 'Setting up Python 3.11 and dependencies. An internet connection is required.'
    $taskUv = Get-SubtitleUv
    $taskPreviousInstallDir = $env:UV_PYTHON_INSTALL_DIR
    try {
        $env:UV_PYTHON_INSTALL_DIR = $taskInstallDir
        Invoke-SubtitleUv -Uv $taskUv -Arguments @('python', 'install', '3.11', '--install-dir', $taskInstallDir, '--no-bin', '--no-registry')
        $taskFound = & $taskUv python find --system --managed-python --no-python-downloads 3.11
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 was not found after installation. Run setup.bat to retry.' }
        $taskBasePython = ([string]$taskFound).Trim()
        $taskResolvedBase = [IO.Path]::GetFullPath($taskBasePython)
        $taskAllowedRoot = [IO.Path]::GetFullPath($taskInstallDir).TrimEnd('\') + '\'
        if (-not $taskResolvedBase.StartsWith($taskAllowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Python was found outside .runtime. Run setup.bat to retry the installation.'
        }
        & $taskBasePython -I -c 'import sys; assert sys.version_info[:2] == (3, 11)'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 could not start. Run setup.bat to retry the installation.' }

        # Repair the app-owned environment in place, preserving installed packages.
        if (Test-Path -LiteralPath $taskEnvironment) {
            $taskConfig = Join-Path $taskEnvironment 'pyvenv.cfg'
            if (Test-Path -LiteralPath $taskConfig) {
                Copy-Item -LiteralPath $taskConfig -Destination ($taskConfig + '.before-repair') -Force
            }
            & $taskBasePython -I -m venv --without-pip --upgrade $taskEnvironment
            if ($LASTEXITCODE -ne 0) { throw 'Runtime is in use or could not be repaired. Close the subtitle app, then retry.' }
        } else {
            Invoke-SubtitleUv -Uv $taskUv -Arguments @('venv', '--python', $taskBasePython, $taskEnvironment)
        }
        Invoke-SubtitleUv -Uv $taskUv -Arguments @('pip', 'install', '--python', $taskPython, '-r', (Join-Path $ProjectRoot 'requirements.txt'))
        if (-not (Test-SubtitlePython -Python $taskPython)) { throw 'Python or a required dependency could not load. Run setup.bat to retry.' }
        Write-Host 'Setup complete.'
        return $taskPython
    } finally {
        if ($null -eq $taskPreviousInstallDir) {
            Remove-Item Env:UV_PYTHON_INSTALL_DIR -ErrorAction SilentlyContinue
        } else {
            $env:UV_PYTHON_INSTALL_DIR = $taskPreviousInstallDir
        }
    }
}
