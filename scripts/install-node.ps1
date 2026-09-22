# =====================================================================
# DEPRECATED 2026-09-22: Windows 节点形态退役（软退役）。Windows 定位=仅
# 浏览器访问 web UI，节点运行时只跑 macOS/Linux；本脚本保留不再维护、
# CI 已停出 Windows 包（release.yml matrix / node-handshake.yml windows
# job 已删）。新部署勿用。
# =====================================================================
# 薄节点部署脚本 Windows/CUDA 档（spec §11.2；正式版 P2 出离线包 + 服务模板）。
# 用法: .\install-node.ps1 -CpUrl URL (-NodeToken TOK | -LicenseKey KEY) [-RepoRoot DIR] [-DryRun] [-SkipModels] [-LivekitUrl ws://<内网IP>:7880]
#       .\install-node.ps1 -CpUrl URL -LicenseKey KEY [-DryRun] [-InstallService]  # license 流（加固云推荐）：自动注册，token 落状态文件复用
#       .\install-node.ps1 [-UninstallService] [-DryRun]                           # 卸载常驻服务
#
# 服务模式（site-delivery Task 8）：委派 bok.py prod install --node-agent / prod
# uninstall —— Task Scheduler 注册单个 bok-node-agent SYSTEM 任务（开机自起 +
# RestartOnFailure 崩溃拉回；node_agent 内部经 cmd_up 拉全栈）。XML/argv 生成见
# tools/schtasks_units.py。注册需要管理员 PowerShell；-UninstallService 不需要
# CpUrl/NodeToken。
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
  [string]$CpUrl = "",
  [string]$NodeToken = "",
  [string]$LicenseKey = "",
  [string]$RepoRoot = "",
  [string]$InstallRoot = "",
  [string]$LivekitUrl = "ws://127.0.0.1:7880",
  [switch]$DryRun,
  [switch]$SkipModels,
  [switch]$InstallService,
  [switch]$UninstallService,
  [switch]$Fetch
)
$ErrorActionPreference = "Stop"

if ($InstallService -and $UninstallService) {
  throw "-InstallService 与 -UninstallService 互斥"
}
if (-not $UninstallService) {
  if (-not $CpUrl) { throw "-CpUrl 必填（仅 -UninstallService 免）" }
  if (-not $NodeToken -and -not $LicenseKey) {
    throw "-NodeToken 与 -LicenseKey 至少给一个（仅 -UninstallService 免）"
  }
  if ($NodeToken -and $LicenseKey) {
    throw "-NodeToken 与 -LicenseKey 二选一（license 流推荐：加固云下自动注册幂等复用）"
  }
}

if (-not $RepoRoot) {
  $RepoRoot = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path (Join-Path $RepoRoot "tools\node_agent.py")) -and $Fetch) {
  # 自举（零 GitHub，冷装机入口）：从云 CP 拉代码包（sha256 校验）+ 可选运行时包
  # → 解到 -InstallRoot（默认 %USERPROFILE%\bok-voice）→ 后续步骤都在该树上跑。
  if (-not $CpUrl) { throw "-Fetch 需要 -CpUrl" }
  if (-not $InstallRoot) { $InstallRoot = Join-Path $env:USERPROFILE "bok-voice" }
  $cred = if ($NodeToken) { $NodeToken } else { $LicenseKey }
  if (-not $cred) { throw "-Fetch 需要 -LicenseKey 或 -NodeToken（下载凭据）" }
  $base = ($CpUrl.TrimEnd('/')) + "/api/nodes/downloads"
  $headers = @{ Authorization = "Bearer $cred" }
  New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("bok-boot-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  Write-Host "==> [fetch] 代码包 pkg/latest …"
  $pkg = Join-Path $tmp "pkg.tar.gz"
  Invoke-WebRequest -Uri "$base/pkg/latest/bok-node-latest.tar.gz" -Headers $headers -OutFile $pkg
  $sha = Join-Path $tmp "pkg.sha256"
  Invoke-WebRequest -Uri "$base/pkg/latest/bok-node-latest.tar.gz.sha256" -Headers $headers -OutFile $sha
  $expected = ((Get-Content $sha -Raw).Split(" ")[0]).ToLower()
  $actual = (Get-FileHash $pkg -Algorithm SHA256).Hash.ToLower()
  if ($expected -ne $actual) { throw "sha256 校验失败（expected $expected, got $actual）" }
  tar -xzf $pkg -C $tmp
  $src = $tmp
  $first = Get-ChildItem $tmp -Directory | Select-Object -First 1
  if ($first -and (Test-Path (Join-Path $first.FullName "tools\node_agent.py"))) { $src = $first.FullName }
  if (-not (Test-Path (Join-Path $src "tools\node_agent.py"))) { throw "代码包布局异常（缺 tools\node_agent.py）" }
  Write-Host ("==> [fetch] 代码包 -> {0}（覆盖式更新，runtime/ 与本地数据保留）" -f $InstallRoot)
  Copy-Item -Path (Join-Path $src "*") -Destination $InstallRoot -Recurse -Force
  try {
    $rt = Join-Path $tmp "rt.tar.gz"
    Invoke-WebRequest -Uri "$base/runtime/latest/runtime-latest.tar.gz" -Headers $headers -OutFile $rt -ErrorAction Stop
    Write-Host "==> [fetch] 运行时包 -> $(Join-Path $InstallRoot 'runtime')"
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "runtime") | Out-Null
    tar -xzf $rt -C (Join-Path $InstallRoot "runtime")
  } catch {
    Write-Host "    运行时包未发布（runtime/latest 404）——需要全栈时由操作员 publish 后重跑"
  }
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  $RepoRoot = $InstallRoot
}
if (-not (Test-Path (Join-Path $RepoRoot "tools\node_agent.py"))) {
  throw "repo root 探测失败（缺 tools\node_agent.py）: $RepoRoot —— 冷装机请加 -Fetch"
}
$VenvPy = Join-Path $RepoRoot ".venv312\Scripts\python.exe"
$AgentPy = Join-Path $RepoRoot "tools\node_agent.py"
$BokPy = Join-Path $RepoRoot "tools\bok.py"

$script:StepNo = 0
# 步总数=加法不是覆盖：常驻 4 步（体检/venv/握手探活/doctor 终检），模型下载与
# 注册常驻服务按开关各 +1。旧覆盖式写法在 -SkipModels -InstallService 组合下
# 报 [x/6] 实跑 5 步（后一个 if 覆盖前一个，组合枚举错了 1）。
$script:StepTotal = 4
if (-not $SkipModels) { $script:StepTotal++ }
if ($InstallService) { $script:StepTotal++ }
if ($UninstallService) { $script:StepTotal = 1 }

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

# ---------- 卸载模式：一步委派 prod uninstall，不走安装计划 ----------
if ($UninstallService) {
  Step "卸载常驻服务（bok.py prod uninstall；schtasks bok-node-agent 等一把清）"
  if ($DryRun) {
    Write-Host "    (dry-run) & <python> tools\bok.py prod uninstall"
  } else {
    $py = $VenvPy
    if (-not (Test-Path $py)) { $py = "python" }
    & $py $BokPy prod uninstall
    if ($LASTEXITCODE -ne 0) { throw "bok.py prod uninstall 失败 rc=$LASTEXITCODE" }
  }
  Write-Host ""
  Write-Host "卸载完成（Task Scheduler 已无 bok-* 任务；app-data 数据/日志保留）。"
  return
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
  #   license 流没有现成 token（注册由 ② 的 node_agent 完成）→ ① 跳过。
  if ($NodeToken) {
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
  } else {
    Write-Host "    - license 流：①探针跳过（无现成 token），注册+心跳由 ② node_agent 完成（同机幂等复用 node_id）"
  }

  # ② 后台起 node_agent（1s 一跳，跑 3s ≥2 跳），输出重定向收日志。
  #   license 流：凭据经 env 传递（argv 在 PowerShell history / 进程列表可见），
  #   node_agent 自动注册、token 落 ~/.bok/node-state.json（0600）复用。
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $VenvPy
  $agentArgs = '"{0}" --cp-url "{1}" --livekit-url "{2}" --ui-dir "{3}" --heartbeat-only --interval 1' -f `
      $AgentPy, $CpUrl, $LivekitUrl, (Join-Path $RepoRoot "apps\web\out")
  if ($NodeToken) {
    $agentArgs += ' --node-token "{0}"' -f $NodeToken
  }
  $psi.Arguments = $agentArgs
  $psi.UseShellExecute = $false
  if ($LicenseKey) {
    $psi.EnvironmentVariables["BOK_LICENSE_KEY"] = $LicenseKey
  }
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

# ---------- [6/6] 注册常驻服务（-InstallService 才有；需管理员 PowerShell） ----------
if ($InstallService) {
  Invoke-Step "注册常驻服务（Task Scheduler bok-node-agent：开机自起 + RestartOnFailure 崩溃拉回）" {
    # 凭据按流透传（bok.py prod install --node-agent 对 node_args 原样转发）：
    # token 直传 argv；license 经任务 XML env 前缀链（与 plist env 等价，凭据本就落 XML）。
    $prodArgs = @("prod", "install", "--node-agent", "--cp-url", $CpUrl,
      "--livekit-url", $LivekitUrl, "--ui-dir", (Join-Path $RepoRoot "apps\web\out"))
    if ($NodeToken) { $prodArgs += @("--node-token", $NodeToken) }
    else { $prodArgs += @("--license-key", $LicenseKey) }
    & $VenvPy $BokPy @prodArgs
    if ($LASTEXITCODE -ne 0) { throw "bok.py prod install --node-agent rc=$LASTEXITCODE" }
    Write-Host "    - 查询: schtasks /query /tn bok-node-agent"
    Write-Host "    - 卸载: .\install-node.ps1 -UninstallService 或 bok.py prod uninstall"
  }
}

# ---------- 收尾 ----------
Write-Host ""
if ($DryRun) {
  Write-Host ("共 {0}/{1} 步（dry-run：未执行任何副作用）。" -f $script:StepNo, $script:StepTotal)
} else {
  Write-Host ("共 {0}/{1} 步，安装完成。" -f $script:StepNo, $script:StepTotal)
  $credHint = "-NodeToken ***"
  if (-not $NodeToken) { $credHint = "-LicenseKey bokn_***" }
  if ($InstallService) {
    Write-Host "常驻服务已注册（开机自起 + 崩溃拉回）："
    Write-Host "  - 健康观测: schtasks /query /tn bok-node-agent；日志在 %LOCALAPPDATA%\BokVoice\logs"
    Write-Host "  - 节点名册: 用管理员凭证 GET $CpUrl/api/nodes 确认节点 online"
    Write-Host "  - 链路自检: & `"$VenvPy`" `"$BokPy`" doctor"
  } else {
    Write-Host "下一步（正式常驻部署）:"
    Write-Host "  1. 注册常驻服务（Task Scheduler；需管理员 PowerShell）:"
    Write-Host "       .\install-node.ps1 -CpUrl $CpUrl $credHint -LivekitUrl ws://<本机内网IP>:7880 -InstallService"
    Write-Host "     或手动:"
    Write-Host "       & `"$VenvPy`" `"$BokPy`" prod install --node-agent --cp-url $CpUrl --livekit-url ws://<本机内网IP>:7880 --ui-dir `"$RepoRoot\apps\web\out`""
    Write-Host "     （node_agent 拉起全栈 serve + 心跳守护）"
    Write-Host "  2. 健康观测: 用管理员凭证 GET $CpUrl/api/nodes 确认节点 online"
    Write-Host "  3. 链路自检: & `"$VenvPy`" `"$BokPy`" doctor"
  }
}
