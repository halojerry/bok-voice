# setup-windows.ps1 — one-time environment bootstrap for the no-Docker Windows path.
# Creates the sidecar venvs + installs the web/realtime deps. Model weights are
# fetched later by `python tools/bok.py download` (or the desktop first-run guide).
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[bok] ensuring Python venvs for sidecars …"
foreach ($name in @("qwen3-asr-sidecar", "qwen3-tts-sidecar")) {
  $venvPy = Join-Path $Root "services\$name\.venv\Scripts\python.exe"
  if (-not (Test-Path $venvPy)) {
    python -m venv (Join-Path $Root "services\$name\.venv")
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -r (Join-Path $Root "services\$name\requirements.txt")
  }
}

Write-Host "[bok] installing web + realtime-translation deps …"
Push-Location (Join-Path $Root "apps\web")
npm install
Pop-Location
Push-Location (Join-Path $Root "services\realtime-translation")
npm install
Pop-Location

# ollama is the LLM backend on Windows.
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Warning "[bok] ollama not on PATH — install from https://ollama.com/download/windows"
}

Write-Host "[bok] Windows setup complete. Next: python tools/bok.py download"

