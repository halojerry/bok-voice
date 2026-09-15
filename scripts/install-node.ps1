# 薄节点部署脚本 Windows/CUDA 档（spec §11.2；正式版 P2 出离线包 + 服务模板）。
# 用法: .\install-node.ps1 -CpUrl URL -NodeToken TOK [-RepoRoot DIR] [-DryRun] [-SkipModels]
#
# 与 scripts/install-node.sh 同一套步骤计划器语义：
#   - 每步带编号执行，任一步失败 throw 并报告第几步（$ErrorActionPreference=Stop）；
#   - -DryRun 只打印计划，零副作用（不建 venv、不下载、不碰 CP、不写 UI 配置）；
#   - [4/5] 握手探活：单发心跳探针（node_token 自鉴权，心跳端点豁免 CP 门禁）
#     → node_agent --heartbeat-only 后台跑 3s → 查 CP 节点列表确认到达 → 清理。
#     节点列表在 auth-on CP 受 root 门禁保护（401），此时优雅降级——探针 200
#     已是「心跳到达」的权威证据（NodeStore 按 token sha256 命中并更新 last_seen）。
#   - GPU 探测与 sh 同款 nvidia-smi 逻辑：有才查、查失败不阻断（草版无探测直接
#     跑 nvidia-smi，无 GPU 机器上 CommandNotFound 会炸掉整个脚本——已修）。
param(
  [Parameter(Mandatory = $true)][string]$CpUrl,
  [Parameter(Mandatory = $true)][string]$NodeToken,
  [string]$RepoRoot = "",
  [switch]$DryRun,
  [switch]$SkipModels
)
$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
  $RepoRoot = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path (Join-Path $RepoRoot "tools\node_agent.py"))) {
  throw "repo root 探测失败（缺 tools\node_agent.py）: $RepoRoot"
}
$VenvPy = Join-Path $RepoRoot ".venv312\Scripts\python.exe"
$AgentPy = Join-Path $RepoRoot "tools\node_agent.py"

$script:StepNo = 0
$script:StepTotal = 5
if ($SkipModels) { $script:StepTotal = 4 }

function Step([string]$desc) {
  $script:StepNo++
  Write-Host ""
  Write-Host ("==> [{0}/{1}] {2}" -f $script:StepNo, $script:StepTotal, $desc)
}

# Invoke-Step：执行原生命令并按步号报错（PS 惯用法：scriptblock + $LASTEXITCODE；
# 先清零，避免纯 PS 语句块沿用上一步残留码造成误判）。
function Invoke-Step([string]$desc, [scriptblock]$action) {
  Step $desc
  if ($DryRun) { Write-Host "    (dry-run) 跳过执行"; return }
  $global:LASTEXITCODE = 0
  & $action
  if ($LASTEXITCODE -ne 0) {
    throw ("第 {0}/{1} 步失败 rc={2}: {3}" -f $script:StepNo, $script:StepTotal, $LASTEXITCODE, $desc)
  }
}

# ---------- [1/5] 环境体检（报告性，不阻断） ----------
Invoke-Step "环境体检: GPU/驱动/磁盘" {
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if ($smi) {
    Write-Host "    - NVIDIA GPU:"
    & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
    if ($LASTEXITCODE -ne 0) { Write-Host "    - nvidia-smi 查询失败（忽略，不影响安装）" }
  } else {
    Write-Host "    - 无 NVIDIA GPU —— CPU 档继续"
  }
  $drive = (Get-Item $RepoRoot).PSDrive
  Write-Host ("    - 磁盘剩余: {0:N1} GB ({1}:)" -f ($drive.Free / 1GB), $drive.Name)
}

# ---------- [2/5] Python venv + 依赖 ----------
Invoke-Step "Python venv + 依赖" {
  if (-not (Test-Path $VenvPy)) {
    Write-Host "    - 创建 .venv312 …"
    python -m venv (Join-Path $RepoRoot ".venv312")
    if ($LASTEXITCODE -ne 0) { throw "python -m venv 失败" }
    & $VenvPy -m pip install --upgrade pip
  }
  $req = Join-Path $RepoRoot "requirements-runtime-win.txt"
  if (Test-Path $req) {
    & $VenvPy -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements-runtime-win.txt 失败" }
  } else {
    Write-Host "    - 未找到 requirements-runtime-win.txt，跳过依赖安装"
  }
  # sidecar venvs（脚本自带存在性检查，幂等；npm/构建工具缺失时仅警告不阻断装节点）
  $setupWin = Join-Path $RepoRoot "scripts\setup-windows.ps1"
  if (Test-Path $setupWin) {
    try {
      & $setupWin
      if ($LASTEXITCODE -ne 0) { throw "setup-windows.ps1 rc=$LASTEXITCODE" }
    } catch {
      Write-Host "    - setup-windows.ps1 失败（sidecar 未就绪，不影响薄节点心跳；需要全栈时再补）: $($_.Exception.Message)"
    }
  } else {
    Write-Host "    - 未找到 scripts\setup-windows.ps1，跳过 sidecar venv"
  }
}

# ---------- [3/5] 模型下载（-SkipModels 可跳） ----------
if (-not $SkipModels) {
  Invoke-Step "模型下载（幂等续传）" {
    & $VenvPy (Join-Path $RepoRoot "tools\bok.py") download
    if ($LASTEXITCODE -ne 0) { throw "bok.py download 失败" }
  }
}

# ---------- [4/5] 节点握手探活 ----------
Step "节点握手探活: 心跳探针 → node_agent 后台 → CP 节点列表确认 → 清理"
if ($DryRun) {
  Write-Host "    (dry-run) ① 单发心跳探针 node_agent.heartbeat_once -> $CpUrl"
  Write-Host "    (dry-run) ② node_agent --heartbeat-only --interval 1 后台跑 3s"
  Write-Host "    (dry-run) ③ GET $CpUrl/api/nodes 确认注册表可见后清理"
} else {
  # ① 单发心跳探针：与守护进程同一条代码路径（node_agent.heartbeat_once）。
  #   心跳端点对 CP 门禁豁免、node_token 自鉴权——200 即「token 有效 + CP 可达 + 到达」。
  $probe = @'
import sys
sys.path.insert(0, sys.argv[3])
from node_agent import NodeConfig, heartbeat_once
ok, body = heartbeat_once(
    NodeConfig(cp_url=sys.argv[1], node_token=sys.argv[2]),
    {"probe": "install-node"},
)
print("[probe] heartbeat -> %s %s" % ("OK" if ok else "FAILED", body))
sys.exit(0 if ok else 1)
'@
  & $VenvPy -c $probe $CpUrl $NodeToken (Join-Path $RepoRoot "tools")
  if ($LASTEXITCODE -ne 0) {
    throw ("第 {0}/{1} 步失败: 心跳探针失败——node_token 无效或 CP 不可达（{2}）" -f $script:StepNo, $script:StepTotal, $CpUrl)
  }

  # ② 后台起 node_agent（1s 一跳，跑 3s ≥2 跳），输出重定向收日志。
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $VenvPy
  $psi.Arguments = ('"{0}" --cp-url "{1}" --node-token "{2}" --ui-dir "{3}" --heartbeat-only --interval 1' -f `
      $AgentPy, $CpUrl, $NodeToken, (Join-Path $RepoRoot "apps\web\out"))
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $daemon = [System.Diagnostics.Process]::Start($psi)
  Start-Sleep -Seconds 3

  # ③ CP 端节点列表确认。auth-on CP 该列表走 root 门禁（401）→ 优雅降级，
  #   ①探针 200 已证到达；auth-off（单机缺省）则直接打印注册表佐证。
  try {
    $rows = Invoke-RestMethod -Uri (($CpUrl.TrimEnd('/')) + "/api/nodes") -Method Get -TimeoutSec 5
    Write-Host "    - CP 节点注册表："
    foreach ($r in @($rows)) {
      $label = $r.name
      if (-not $label) { $label = $r.node_id }
      Write-Host ("      * {0} [{1}] last_seen={2}" -f $label, $r.status, $r.last_seen_at)
    }
  } catch {
    Write-Host "    - 节点列表未开放读取（$($_.Exception.Message)）——root 门禁保护；心跳到达已由 ① 探针证实"
  }

  # ④ 清理：杀后台、验日志无心跳失败（3s 内 1s 间隔必有 ≥2 跳，失败必留痕）。
  if (-not $daemon.HasExited) { $null = $daemon.Kill() }
  $daemonLog = $daemon.StandardOutput.ReadToEnd() + $daemon.StandardError.ReadToEnd()
  if ($daemonLog -match "heartbeat failed") {
    Write-Host $daemonLog
    throw ("第 {0}/{1} 步失败: node_agent 后台心跳出现失败日志" -f $script:StepNo, $script:StepTotal)
  }
  Write-Host "    - node_agent 后台心跳 3s 无失败；runtime-config.js 已注入 apps\web\out"
}

# ---------- [5/5] doctor 终检（报告性，不阻断——正式版将作硬门禁） ----------
Step "doctor 终检（报告性，不阻断）"
if ($DryRun) {
  Write-Host "    (dry-run) $VenvPy tools\bok.py doctor"
} else {
  & $VenvPy (Join-Path $RepoRoot "tools\bok.py") doctor
  if ($LASTEXITCODE -ne 0) {
    Write-Host ("    - doctor 退出码 {0} —— 安装完成，建议按 doctor 输出人工复查" -f $LASTEXITCODE)
  }
}

# ---------- 收尾 ----------
Write-Host ""
if ($DryRun) {
  Write-Host ("共 {0}/{1} 步（dry-run：未执行任何副作用）。" -f $script:StepNo, $script:StepTotal)
} else {
  Write-Host ("共 {0}/{1} 步，安装完成。" -f $script:StepNo, $script:StepTotal)
  Write-Host "下一步（正式常驻部署；Windows 服务/NSSM 模板在后续轮提供）:"
  Write-Host "  1. 常驻心跳 + 全栈:"
  Write-Host "       & `"$VenvPy`" `"$AgentPy`" --cp-url $CpUrl --node-token *** --ui-dir `"$RepoRoot\apps\web\out`" --interval 60"
  Write-Host "     （不带 --heartbeat-only 即拉起全栈 serve + 心跳守护）"
  Write-Host "  2. 健康观测: 用管理员凭证 GET $CpUrl/api/nodes 确认节点 online"
  Write-Host "  3. 链路自检: & `"$VenvPy`" `"$RepoRoot\tools\bok.py`" doctor"
}
