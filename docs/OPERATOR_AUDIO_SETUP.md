# 话务员/译员机器虚拟声卡设置（B 线同传路由）

> 只影响 **B 线同传**（把 AI 译文以「麦克风」身份喂给会议软件）；A 线客服通话
> 走 LiveKit WebRTC，不需要虚拟声卡。

## 为什么需要

同传场景里会议软件（Zoom/Teams/腾讯会议/微信）只认「麦克风输入」。AI 译文的
TTS 出声是「扬声器输出」——中间要一条**虚拟音频线**把输出回路成输入：

```
AI 译文 TTS ──播放到──> [虚拟声卡输入端(扬声器)] ==同卡直通==> [虚拟声卡输出端(麦克风)] ──会议软件选它作麦克风
```

两平台生态**完全不同，不能共用**：

| 平台 | 事实标准 | 许可 | 安装形态 |
|---|---|---|---|
| macOS | **BlackHole 2ch** | GPL-3.0（自由装/分发） | pkg 或 `brew install blackhole-2ch`，要 sudo |
| Windows | **VB-CABLE**（VB-Audio Virtual Cable） | donationware（个人免费/商用捐赠；**安装器再分发需 VB-Audio 允许**） | 驱动级，必须管理员提权，官方安装器无静默开关（GUI 两下点击） |

> 分发合规：正因为 VB-CABLE 的再分发限制，我们的脚本**下载即装、不把安装器打进
> MSI/安装包**——产品只带检测与引导，驱动本体始终从 VB-Audio 官方下载。

## 一键脚本

```bash
# macOS（brew 在场则直接装；无 brew 打开官方下载页并给两步指引）
scripts/setup-virtual-audio.sh            # 加 --dry-run 只看计划

# Windows（PowerShell，普通权限起，装驱动那步自动 UAC 提权）
powershell -ExecutionPolicy Bypass -File scripts\setup-virtual-audio.ps1
```

脚本行为：检测 →（缺失则）安装/引导 → 复检。装好后设备名：

- macOS：`BlackHole 2ch`
- Windows：一对设备 `CABLE Input`（扬声器侧，播放目标）/ `CABLE Output`（麦克风侧，会议软件里选它）

## 装完必做

1. **重启浏览器**（音频设备列表是页面加载时的快照，不重启看不到新设备）；
2. 会议软件里把麦克风设为 **CABLE Output**（Windows）/**BlackHole 2ch**（macOS）；
3. 本产品同传页里把译文输出设备选为 **CABLE Input / BlackHole 2ch**；
4. 自检：`python tools/bok.py doctor` 应报 `virtual audio: ok`。

## 常见坑

- **听到自己译文回声**：会议软件同时选了 CABLE Output 做麦克风和扬声器——扬声器
  必须选真实耳机/音箱，只有麦克风用虚拟卡；
- Windows 装完检测不到：PnP 枚举有延迟，等几秒重跑脚本或重启系统；
- macOS `brew install` 卡在 sudo：脚本在终端跑（有交互 tty）才能要密码。
