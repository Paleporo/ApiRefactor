# Setup completo in un comando (Windows / PowerShell): .venv + dipendenze Python + tool Node locali.
# Uso:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "==> uv sync (crea .venv e installa dipendenze + dev)"
    uv sync --extra server
} else {
    $pyExe = $null; $pyArgs = @()
    foreach ($c in @(@("py", "-3.13"), @("py", "-3.12"), @("python"))) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $pyExe = $exe; $pyArgs = $pre; break }
        }
    }
    if (-not $pyExe) { throw "Serve Python >= 3.12 (oppure installa uv: https://docs.astral.sh/uv/)" }
    Write-Host "==> uv non trovato: fallback $pyExe $pyArgs -m venv + pip"
    & $pyExe @pyArgs -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    & .\.venv\Scripts\python.exe -m pip install -e . --no-deps
}

if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "==> npm install (Spectral + swagger2openapi in .\node_modules\.bin)"
    npm install --no-fund --no-audit
} else {
    Write-Warning "npm non trovato. Installa Node.js >= 18, poi: npm install (oppure npm install -g @stoplight/spectral-cli swagger2openapi)"
}
Write-Host "==> Fatto. Verifica: .\.venv\Scripts\python.exe -m pytest"
