# 薄节点部署草版 Windows/CUDA 档（spec §11.2；正式版 P2 出离线包）。
# 用法: .\install-node.ps1 -CpUrl URL -NodeToken TOK [-DryRun] [-SkipModels]
param(
  [Parameter(Mandatory=$true)][string]$CpUrl,
  [Parameter(Mandatory=$true)][string]$NodeToken,
  [switch]$DryRun,
  [switch]$SkipModels
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Step($desc, $cmd) {
  Write-Host "[plan] $desc"
  if (-not $DryRun) { Invoke-Expression $cmd }
}

Step "[1/5] 环境体检: GPU/驱动" "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
Step "[2/5] Python venv + 依赖" "& `"$RepoRoot\scripts\bootstrap.sh`""   # P0 草版占位: Windows 走 scripts/bootstrap.ps1（若无则本步提示手工执行 requirements 安装）
if (-not $SkipModels) {
  Step "[3/5] 模型下载" "& `"$RepoRoot\.venv312\Scripts\python.exe`" `"$RepoRoot\tools\bok.py`" download"
}
Step "[4/5] 节点注册+心跳/UI 注入" "& `"$RepoRoot\.venv312\Scripts\python.exe`" `"$RepoRoot\tools\node_agent.py`" --cp-url `"$CpUrl`" --node-token `"$NodeToken`" --ui-dir `"$RepoRoot\apps\web\out`" --heartbeat-only --interval 1"
Step "[5/5] doctor 终检" "& `"$RepoRoot\.venv312\Scripts\python.exe`" `"$RepoRoot\tools\bok.py`" doctor"
Write-Host "完成（DryRun=$DryRun）"
