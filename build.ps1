param([string]$Python = "")
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $Python) {
    if (Test-Path '.venv\Scripts\python.exe') { $Python = (Resolve-Path '.venv\Scripts\python.exe').Path }
    elseif (Test-Path '..\.venv\Scripts\python.exe') { $Python = (Resolve-Path '..\.venv\Scripts\python.exe').Path }
    else { $Python = (Get-Command python).Source }
}
& $Python make_icon.py
& $Python -m PyInstaller --noconfirm --windowed --onedir --name Shijian --distpath release --icon static\app.ico --add-data 'static;static' --collect-all webview --collect-all pypdfium2 --collect-all pypdfium2_raw --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on desktop.py
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed.' }
Copy-Item -LiteralPath 'README.md' -Destination 'release\Shijian\使用说明.md'
Copy-Item -LiteralPath 'install-mineru.ps1' -Destination 'release\Shijian\install-mineru.ps1'
Copy-Item -LiteralPath 'install-mineru.cmd' -Destination 'release\Shijian\安装本地识别引擎.cmd'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    New-Item -ItemType Directory -Path 'release\Shijian\tools' -Force | Out-Null
    Copy-Item -LiteralPath $uv.Source -Destination 'release\Shijian\tools\uv.exe'
    Copy-Item -LiteralPath 'licenses\uv-LICENSE-MIT' -Destination 'release\Shijian\tools\uv-LICENSE-MIT'
    Copy-Item -LiteralPath 'licenses\uv-LICENSE-APACHE' -Destination 'release\Shijian\tools\uv-LICENSE-APACHE'
}
Write-Output 'Built: release\Shijian\Shijian.exe'
