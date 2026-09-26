param([string]$Python = "", [string]$BuildOutput = "release/current")
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$outputRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $BuildOutput))
$projectPrefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
if (-not $outputRoot.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output must be inside the project directory.'
}
$packageRoot = Join-Path $outputRoot 'Shijian'
$packageExe = Join-Path $packageRoot 'Shijian.exe'
if (Get-Process -Name Shijian -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $packageExe }) {
    throw 'The build target is running. Close that version of Shijian before rebuilding.'
}
if (-not $Python) {
    if (Test-Path '.venv\Scripts\python.exe') { $Python = (Resolve-Path '.venv\Scripts\python.exe').Path }
    elseif (Test-Path '..\.venv\Scripts\python.exe') { $Python = (Resolve-Path '..\.venv\Scripts\python.exe').Path }
    else { $Python = (Get-Command python).Source }
}
& $Python make_icon.py
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
& $Python -m PyInstaller --noconfirm --windowed --onedir --name Shijian --distpath $outputRoot --icon static\app.ico --add-data 'static;static' --collect-all webview --collect-all pypdfium2 --collect-all pypdfium2_raw --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on desktop.py
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed.' }
Copy-Item -LiteralPath 'README.md' -Destination (Join-Path $packageRoot '使用说明.md')
Copy-Item -LiteralPath 'install-mineru.ps1' -Destination (Join-Path $packageRoot 'install-mineru.ps1')
Copy-Item -LiteralPath 'install-mineru.cmd' -Destination (Join-Path $packageRoot '安装本地识别引擎.cmd')
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    $toolsRoot = Join-Path $packageRoot 'tools'
    New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
    Copy-Item -LiteralPath $uv.Source -Destination (Join-Path $toolsRoot 'uv.exe')
    Copy-Item -LiteralPath 'licenses\uv-LICENSE-MIT' -Destination (Join-Path $toolsRoot 'uv-LICENSE-MIT')
    Copy-Item -LiteralPath 'licenses\uv-LICENSE-APACHE' -Destination (Join-Path $toolsRoot 'uv-LICENSE-APACHE')
}
Write-Output "Built: $packageExe"
