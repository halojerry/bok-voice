# =====================================================================
# DEPRECATED 2026-09-22: Windows 节点形态退役（软退役）。Windows 定位=仅
# 浏览器访问 web UI，节点运行时只跑 macOS/Linux；本脚本保留不再维护、
# CI 已停出 Windows 包（release.yml matrix / node-handshake.yml windows
# job 已删）。新部署勿用。
# =====================================================================
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
    & $venvPy -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 -r (Join-Path $Root "requirements-runtime-win.txt")
  }
}

Write-Host "[bok] installing web + realtime-translation deps …"
Push-Location (Join-Path $Root "apps\web")
npm ci
Pop-Location
Push-Location (Join-Path $Root "services\realtime-translation")
npm ci
Pop-Location

Write-Host "[bok] Windows setup complete. Next: python tools/bok.py download"
