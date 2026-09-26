# Downloads the engine and its dependencies only. No manuscripts are uploaded.
$ErrorActionPreference = 'Stop'
$engineRoot = Join-Path $env:LOCALAPPDATA 'Shijian\.mineru'
$bundledUv = Join-Path $PSScriptRoot 'tools\uv.exe'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $bundledUv) { $uvCommand = Get-Command $bundledUv }
if ($uvCommand) {
    if (-not (Test-Path -LiteralPath (Join-Path $engineRoot 'Scripts\python.exe'))) {
        & $uvCommand.Source venv --python 3.12 $engineRoot
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    & $uvCommand.Source pip install --python (Join-Path $engineRoot 'Scripts\python.exe') 'mineru==4.0.7'
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Please install Python 3.12 or uv first, then run this script again.' }
    if (-not (Test-Path -LiteralPath (Join-Path $engineRoot 'Scripts\python.exe'))) {
        & $pythonCommand.Source -m venv $engineRoot
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    & (Join-Path $engineRoot 'Scripts\python.exe') -m pip install --upgrade pip
    & (Join-Path $engineRoot 'Scripts\python.exe') -m pip install 'mineru==4.0.7'
}
if ($LASTEXITCODE -ne 0) { throw 'MinerU installation failed. Check your network and retry.' }
Write-Output "Installed: $(Join-Path $engineRoot 'Scripts\mineru-kit.exe')"
Write-Output 'Restart Shijian. The first OCR run downloads model weights and can take several minutes.'
