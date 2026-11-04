$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw '.NET Framework 4.x C# compiler not found. Build on Windows x64.'
}

$build = Join-Path $root 'build\launcher'
$name = 'Real-Time Subtitle Translator.exe'
$output = Join-Path $build $name
New-Item -ItemType Directory -Path $build -Force | Out-Null
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
    "/out:$output" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) {
    throw 'Launcher build failed.'
}
Copy-Item -LiteralPath $output -Destination (Join-Path $root $name) -Force
Write-Host "Built $name"
