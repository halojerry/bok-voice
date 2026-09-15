# Windows 虚拟声卡（VB-CABLE）检测 + 引导安装——B 线同传把 TTS 译文路由成
# 「麦克风」喂会议软件（Zoom/Teams/微信会议）；A 线通话不需要。
#
# 用法（PowerShell，普通权限运行即可，装驱动那步自动提权）:
#   powershell -ExecutionPolicy Bypass -File setup-virtual-audio.ps1 [-DryRun]
#
# 平台事实（与 macOS 侧 BlackHole 完全不同）:
#   - Windows 的事实标准是 VB-Audio 的 VB-CABLE：装后出现一对设备
#     「CABLE Input」（扬声器侧，播放给它）+「CABLE Output」（麦克风侧，会议软件选它）
#   - 驱动级安装：必须管理员提权；官方安装器无静默开关（GUI 两下点击）
#   - 许可=donationware：个人免费、商用需捐赠/授权，安装器再分发需 VB-Audio 允许
#     ——因此本脚本【下载即装、不随包分发安装器】，规避再分发问题
#
param([switch]$DryRun)

function Say($m) { Write-Host "[audio-setup] $m" -ForegroundColor Magenta }

function Detect {
    # 装好后在 Win32_SoundDevice 出现 "VB-Audio Virtual Cable"；AudioEndpoint
    # 侧的名字是 CABLE Input/CABLE Output。两处任一命中即算已装。
    $devs = Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "VB-Audio|Virtual Cable" }
    if ($devs) { return $true }
    try {
        $eps = Get-PnpDevice -Class AudioEndpoint -ErrorAction Stop |
            Where-Object { $_.FriendlyName -match "CABLE Input|CABLE Output" }
        return [bool]$eps
    } catch { return $false }
}

Say "检测音频设备……"
if (Detect) {
    Say "[OK] VB-CABLE 已安装（B 线同传路由可用）。当前相关设备："
    Get-CimInstance Win32_SoundDevice | Where-Object { $_.Name -match "VB-Audio|CABLE" } |
        Select-Object -ExpandProperty Name
    exit 0
}
Say "[MISS] 未检出 VB-CABLE 虚拟声卡。"

$setup = Join-Path $env:TEMP "VBCABLE_Setup_x64.exe"
$url = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Setup_x64.exe"

if ($DryRun) {
    Say "(dry-run) 将执行: 下载 $url -> 提权运行安装器（GUI 两下点击）-> 复检设备"
    exit 0
}

Say "下载官方安装器（下载即装、不随产品分发——VB-CABLE 是 donationware）……"
try {
    # TLS1.2（老系统默认不开）+ 安全协议下载
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing
} catch {
    Say "下载失败（$($_.Exception.Message)）——手动路径："
    Say "  1. 浏览器打开 https://vb-audio.com/Cable/ 下载 VBCABLE_DriverPack"
    Say "  2. 解压后以管理员运行 VBCABLE_Setup_x64.exe，点 Install Driver"
    Say "  3. 装完重启浏览器，重跑本脚本复检"
    exit 1
}

Say "提权运行安装器——弹出 UAC 后在安装器里点「Install Driver」（约两下点击）……"
try {
    # 官方安装器无静默开关；-Verb RunAs 负责 UAC 提权，-Wait 等用户点完
    Start-Process -FilePath $setup -Verb RunAs -Wait
} catch {
    Say "提权被取消/失败（$($_.Exception.Message)）——可手动以管理员运行: $setup"
    exit 1
}

Start-Sleep -Seconds 3
if (Detect) {
    Say "[OK] 安装成功：CABLE Input（扬声器侧）/ CABLE Output（麦克风侧）已就绪。"
    Say "重启浏览器后生效（设备列表在页面加载时快照）；会议软件里选「CABLE Output」作麦克风。"
} else {
    Say "[WARN] 未立即检出（PnP 枚举有延迟）——等几秒重跑本脚本复检；仍无则重启系统后再检。"
}
