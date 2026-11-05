$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot
$pythonCandidates = @(
    (Join-Path $PSScriptRoot '.runtime\python\tools\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe')
)
$pythonPath = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonPath) {
    Write-Host 'Baixando distribuicao oficial do Python para a pasta do projeto...'
    $runtimeDir = Join-Path $PSScriptRoot '.runtime'
    [System.IO.Directory]::CreateDirectory($runtimeDir) | Out-Null
    $archivePath = Join-Path $runtimeDir 'python.3.13.17.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://api.nuget.org/v3-flatcontainer/python/3.13.17/python.3.13.17.nupkg' -OutFile $archivePath
    Expand-Archive -LiteralPath $archivePath -DestinationPath (Join-Path $runtimeDir 'python') -Force
    $pythonPath = Join-Path $runtimeDir 'python\tools\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python nao encontrado no pacote.' }
}
if (-not (Test-Path -LiteralPath '.\.venv\Scripts\python.exe')) {
    & $pythonPath -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar ambiente Python.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependencias.' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.browsers'
& '.\.venv\Scripts\python.exe' -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar o navegador.' }
Write-Host 'Instalado. Abra iniciar.cmd para escolher a quantidade e comecar.'
