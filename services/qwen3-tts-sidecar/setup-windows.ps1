$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = if ($env:QWEN3_TTS_VENV) { $env:QWEN3_TTS_VENV } else { Join-Path $Root ".venv" }
python -m venv $Venv
& (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip
& (Join-Path $Venv "Scripts\python.exe") -m pip install -r (Join-Path $Root "requirements.txt")
Write-Host "Qwen3-TTS sidecar ready. Start with:"
Write-Host "  $($Venv)\Scripts\uvicorn.exe app:app --app-dir `"$Root`" --host 0.0.0.0 --port 8788"
