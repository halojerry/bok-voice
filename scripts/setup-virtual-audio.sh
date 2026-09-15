#!/usr/bin/env bash
# macOS 虚拟声卡（BlackHole 2ch）检测+引导安装——B 线同传把 TTS 译文路由成
# 「麦克风」喂会议软件（Zoom/Teams/微信会议）用；A 线通话不需要。
#
# 用法: setup-virtual-audio.sh [--dry-run]
#   检测 → 缺失且有 Homebrew → brew install blackhole-2ch（装驱动要 sudo 密码）
#        → 无 brew → 打开官方 pkg 下载页 + 打印两步指引
#        → 复检确认设备出现
#
# 许可：BlackHole 2ch 是 GPL-3.0（可自由安装分发），与 Windows 侧的 VB-CABLE
# （donationware，见 ps1 版注释）不同。
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

say() { printf '\033[1;35m[audio-setup]\033[0m %s\n' "$*"; }

detect() {
  # system_profiler 列全部音频设备；BlackHole 装好后以「BlackHole 2ch」出现。
  system_profiler SPAudioDataType 2>/dev/null | grep -qi "blackhole" 
}

say "检测音频设备……"
if detect; then
  say "✅ BlackHole 已安装（B 线同传路由可用）。当前设备："
  system_profiler SPAudioDataType | grep -E "^\s+[A-Z0-9].*:" | head -8
  exit 0
fi
say "❌ 未检出 BlackHole 虚拟声卡。"

if [[ $DRY_RUN -eq 1 ]]; then
  say "(dry-run) 将执行: $(command -v brew >/dev/null && echo 'brew install blackhole-2ch' || echo '打开官方下载页 https://existential.audio/blackhole/ 并指引 pkg 安装')"
  exit 0
fi

if command -v brew >/dev/null; then
  say "走 Homebrew 安装（装驱动需输入 sudo 密码）……"
  brew install blackhole-2ch
else
  say "未找到 Homebrew——打开官方下载页，装法两步："
  open "https://existential.audio/blackhole/" 2>/dev/null || true
  cat <<'EOF'
  1. 下载 BlackHole 2ch pkg 并双击安装（输入密码）；
  2. 重启浏览器（音频设备列表在页面加载时快照，不重启看不到新设备）。
  装完重跑本脚本复检。
EOF
  exit 0
fi

sleep 2
if detect; then
  say "✅ 安装成功，BlackHole 2ch 已可用。记得重启浏览器后到同传页选择设备。"
else
  say "⚠️ 未立即检出（system_profiler 有缓存延迟）——拔插一次音频设备或稍后重跑本脚本复检。"
fi
