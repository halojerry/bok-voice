# =====================================================================
# DEPRECATED 2026-09-22: Windows 节点形态退役（软退役）。Windows 定位=仅
# 浏览器访问 web UI，节点运行时只跑 macOS/Linux；本脚本保留不再维护、
# CI 已停出 Windows 包（release.yml matrix / node-handshake.yml windows
# job 已删）。新部署勿用。
# =====================================================================
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = if ($env:QWEN3_ASR_VENV) { $env:QWEN3_ASR_VENV } else { Join-Path $Root ".venv" }
python -m venv $Venv
& (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip
& (Join-Path $Venv "Scripts\python.exe") -m pip install -r (Join-Path $Root "requirements.txt")
Write-Host "Qwen3-ASR sidecar ready. Start with:"
Write-Host "  $($Venv)\Scripts\uvicorn.exe app:app --app-dir `"$Root`" --host 0.0.0.0 --port 8787"
