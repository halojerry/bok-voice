#!/usr/bin/env bash
# 节点装机脚本（Ubuntu/GPU 主档，2026-09-20 七步制正式化）。
# 用法: install-node.sh --cp-url URL (--node-token TOK | --license-key KEY)
#         [--repo-root DIR] [--livekit-url ws://<内网IP>:7880]
#         [--models min|a|all|"键 键 ..."] [--yes] [--no-service]
#         [--bind ADDR[,ADDR]] [--webhook-url URL] [--dry-run] [--skip-models]
#
# 七步（任一步失败即停机并报告第几步；--dry-run 零副作用）：
#   [1/7] 环境体检：GPU/VRAM 分档 + 磁盘 + 端口预检 + CP 可达性
#   [2/7] venv + 依赖 + Linux CUDA 栈（幂等；已装跳过）
#   [3/7] license 注册 + 心跳探活（提前——坏 key 秒失败，节点即刻在云台可见）
#   [4/7] 模型选型 + 下载（预设档按 VRAM 给建议；--models/--yes 无人值守）
#   [5/7] systemd 常驻安装（root 直接装载；非 root 打印分步命令）
#   [6/7] 拉起全栈 + 六点真自检（**硬门禁**：不过=装机失败，不印成功摘要）
#   [7/7] 成功摘要：内网 IP 自动探测 + 入口 URL（中性词，不打印任何凭据）
#
# 约定：术语与打印面不出现实现栈名（客户面中性表述）；凭据只经 env/状态文件，
# 不进 argv、不进摘要。
set -euo pipefail

usage() {
  cat <<'EOF'
用法: install-node.sh --cp-url URL (--node-token TOK | --license-key KEY) [选项]

  --cp-url       控制面基址（必填，如 https://cp.example.com）
  --node-token   节点令牌（与 --license-key 二选一）
  --license-key  license 流（推荐）：自动注册（同机幂等复用 node_id），token 落
                 ~/.bok/node-state.json（0600）重启复用不烧配额
  --repo-root    仓库根目录（缺省自动探测为本脚本上级目录）
  --livekit-url  节点媒体服务地址（默认 ws://127.0.0.1:7880；**内网多话务员必填
                 本机内网 IP** 如 ws://192.168.1.10:7880，否则只有节点本机能用）
  --bind         媒体服务监听地址（默认=内网 IP 自动探测；逗号分隔多址）
  --webhook-url  事件回调地址（默认 CP_URL/api/webhook/livekit——节点形态必须指
                 云 CP；指向本地 CP 的回调在节点上不存在）
  --models       模型档：min（最小可用）/ a（+纪要档）/ all（+同传档）/
                 或空格分隔键列表（asr tts_preset tts_clone llm …）
  --yes          无人值守：不交互，全采纳探测建议
  --no-service   跳过常驻安装（手工档）
  --dry-run      只打印计划，零副作用
  --skip-models  跳过模型下载（已在别处下载过时用）
EOF
}

CP_URL=""; NODE_TOKEN=""; LICENSE_KEY=""; DRY_RUN=0; SKIP_MODELS=0; REPO_ROOT_ARG=""
LIVEKIT_URL="ws://127.0.0.1:7880"; LIVEKIT_BIND=""; WEBHOOK_URL=""; MODELS_SEL=""
ASSUME_YES=0; INSTALL_SERVICE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cp-url) CP_URL="${2:-}"; shift 2;;
    --livekit-url) LIVEKIT_URL="${2:-}"; shift 2;;
    --bind) LIVEKIT_BIND="${2:-}"; shift 2;;
    --webhook-url) WEBHOOK_URL="${2:-}"; shift 2;;
    --models) MODELS_SEL="${2:-}"; shift 2;;
    --yes) ASSUME_YES=1; shift;;
    --no-service) INSTALL_SERVICE=0; shift;;
    --node-token) NODE_TOKEN="${2:-}"; shift 2;;
    --license-key) LICENSE_KEY="${2:-}"; shift 2;;
    --repo-root) REPO_ROOT_ARG="${2:-}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --skip-models) SKIP_MODELS=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2;;
  esac
done
[[ -n "$CP_URL" ]] || { echo "--cp-url required" >&2; exit 2; }
[[ -n "$NODE_TOKEN" || -n "$LICENSE_KEY" ]] || {
  echo "--node-token 或 --license-key 至少给一个" >&2; exit 2;
}
CP_URL="${CP_URL%/}"

REPO_ROOT="${REPO_ROOT_ARG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[[ -d "$REPO_ROOT/tools" && -f "$REPO_ROOT/scripts/bootstrap.sh" ]] || {
  echo "repo root 探测失败（缺 tools/ 或 scripts/bootstrap.sh）: $REPO_ROOT" >&2; exit 2;
}
PY="$REPO_ROOT/.venv312/bin/python"

STEP_NO=0
STEP_TOTAL=7
AGENT_PID=""
SEL_MODELS=""
VRAM_MB=0

step() { STEP_NO=$((STEP_NO+1)); echo; echo "==> [${STEP_NO}/${STEP_TOTAL}] $*"; }
note() { echo "    - $*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "    (dry-run) $*"; return 0; fi
  local rc=0
  "$@" || rc=$?
  if (( rc != 0 )); then
    echo "!! [${STEP_NO}/${STEP_TOTAL}] 第 ${STEP_NO} 步失败 rc=${rc}: $*" >&2
    exit "$rc"
  fi
}
cleanup_agent() { [[ -n "$AGENT_PID" ]] && kill "$AGENT_PID" 2>/dev/null || true; }

# 内网 IP 探测：优先默认路由源地址（多网卡机器拿真实出口网卡），兜底 hostname -I
# 首项；都拿不到返回空（调用方回退 127.0.0.1 并告警）。
lan_ip() {
  local ip=""
  ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1 || true)"
  [[ -n "$ip" ]] || ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  # macOS 兜底（本脚本主档是 Ubuntu；mac 用于开发机干跑/演示）
  [[ -n "$ip" ]] || ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  printf '%s' "$ip"
}

# ---------- [1/7] 环境体检（报告性，不阻断） ----------
step "环境体检: GPU/VRAM 分档 + 磁盘 + 端口预检 + CP 可达性"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    (dry-run) nvidia-smi 分档 + df -h + 端口占用扫描 + GET $CP_URL/health"
else
  if command -v nvidia-smi >/dev/null 2>&1; then
    note "GPU:"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader \
      || note "nvidia-smi 查询失败（忽略，不影响安装）"
    VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9' || true)"
    VRAM_MB="${VRAM_MB:-0}"
  else
    note "无 NVIDIA GPU —— 本档位面向 GPU 服务器；缺 GPU 时推理不可用"
  fi
  note "磁盘（\$HOME 所在卷）: $(df -h "$HOME" | tail -1)"
  # 端口预检：随后要拉起这些端口，被占用会以「起不来」形式在第 6 步暴露——
  # 提前警示省一轮排查（只报告，不阻断）。
  busy=""
  for p in 3000 7880 7881 7882 8081 8082 8083 8787 8788 8790 1235 1236 1237; do
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${p}$"; then
      busy="$busy $p"
    fi
  done
  if [[ -n "$busy" ]]; then
    note "端口占用（预期空闲）:$busy —— 若非本机既有 Bok 栈，请先腾退"
  fi
  # CP 可达性：第 3 步注册的前置，提前暴露 DNS/防火墙问题。
  if curl -fsS --max-time 8 "$CP_URL/health" >/dev/null 2>&1; then
    note "控制面可达: $CP_URL/health ok"
  else
    note "控制面不可达: $CP_URL/health —— 若为网络问题，第 3 步注册会失败"
  fi
fi

# ---------- [2/7] Python venv + 依赖 + CUDA 栈 ----------
step "Python venv + 依赖 + Linux CUDA 栈（幂等）"
run "$REPO_ROOT/scripts/bootstrap.sh"
CUDA_REQ="$REPO_ROOT/requirements-runtime-linux-cuda.txt"
RT_PY="$REPO_ROOT/runtime/python/bin/python3"
if [[ "$(uname -s)" != "Linux" || ! -f "$CUDA_REQ" || ! -x "$RT_PY" ]]; then
  note "非 Linux / 无 runtime python / 无 cuda requirements —— 本步跳过（mac-win 包自带全量）"
elif [[ $DRY_RUN -eq 1 ]]; then
  echo "    (dry-run) $RT_PY -m pip install -r $CUDA_REQ"
else
  # 幂等门：runtime python 已能 import torch 即视为 CUDA 栈在位（平台镜像预装/
  # 重复装机都走这条快路）。镜像经标准 PIP_INDEX_URL/PIP_EXTRA_INDEX_URL 透传。
  if "$RT_PY" -c 'import torch' >/dev/null 2>&1; then
    note "runtime python 已有 torch —— CUDA 栈在位，跳过（$("$RT_PY" -c 'import torch; print(torch.__version__)' 2>/dev/null || echo '?')）"
  else
    note "装入 CUDA 栈（torch/transformers/qwen-asr…，下载约 3GB，耐心）"
    run "$RT_PY" -m pip install --no-cache-dir -r "$CUDA_REQ"
  fi
fi

# ---------- [3/7] license 注册 + 心跳探活（提前：坏 key 秒失败） ----------
step "license 注册 + 心跳探活"
if [[ $DRY_RUN -eq 1 ]]; then
  note "(dry-run) ① 单发心跳探针 node_agent.heartbeat_once -> $CP_URL"
  note "(dry-run) ② node_agent --heartbeat-only --interval 1 后台跑 3s"
  note "(dry-run) ③ 失败日志零命中即通过"
else
  # ① 单发心跳探针：与守护进程同一条代码路径（node_agent.heartbeat_once）。
  #   心跳端点对控制面门禁豁免、node_token 自鉴权——200 即「token 有效 + 可达 + 到达」。
  #   license 流没有现成 token（注册由 ② 完成）→ ① 跳过。
  probe_rc=0
  if [[ -n "$NODE_TOKEN" ]]; then
    PYTHONPATH="$REPO_ROOT/tools" "$PY" -c '
import sys
from node_agent import NodeConfig, heartbeat_once
ok, body = heartbeat_once(NodeConfig(cp_url=sys.argv[1], node_token=sys.argv[2]),
                          {"probe": "install-node"})
print("[probe] heartbeat -> %s %s" % ("OK" if ok else "FAILED", body))
sys.exit(0 if ok else 1)
' "$CP_URL" "$NODE_TOKEN" || probe_rc=$?
    if (( probe_rc != 0 )); then
      echo "!! [${STEP_NO}/${STEP_TOTAL}] 心跳探针失败：node_token 无效或控制面不可达（${CP_URL}）" >&2
      exit "$probe_rc"
    fi
  else
    note "license 流：①探针跳过（无现成 token），注册+心跳由 ② 完成"
  fi
  # ② 后台起 node_agent（1s 一跳，跑 3s ≥2 跳），失败日志落临时文件。
  agent_log="$(mktemp "${TMPDIR:-/tmp}/bok-node-agent.XXXXXX")"
  agent_args=(--cp-url "$CP_URL" --livekit-url "$LIVEKIT_URL" \
    --ui-dir "$REPO_ROOT/apps/web/out" --heartbeat-only --interval 1)
  if [[ -n "$NODE_TOKEN" ]]; then
    "$PY" "$REPO_ROOT/tools/node_agent.py" "${agent_args[@]}" \
      --node-token "$NODE_TOKEN" >"$agent_log" 2>&1 &
  else
    # license 经 env 传递：argv 在 ps/shell history 可见。
    BOK_LICENSE_KEY="$LICENSE_KEY" "$PY" "$REPO_ROOT/tools/node_agent.py" \
      "${agent_args[@]}" >"$agent_log" 2>&1 &
  fi
  AGENT_PID=$!
  trap cleanup_agent EXIT
  sleep 3
  agent_pid="$AGENT_PID"
  cleanup_agent
  AGENT_PID=""
  trap - EXIT
  wait "$agent_pid" 2>/dev/null || true
  if grep -q "heartbeat failed" "$agent_log" 2>/dev/null; then
    echo "!! [${STEP_NO}/${STEP_TOTAL}] node_agent 后台心跳出现失败日志:" >&2
    cat "$agent_log" >&2
    rm -f "$agent_log"
    exit 1
  fi
  rm -f "$agent_log"
  note "注册/心跳通过（节点已在控制台可见）；token 已落 ~/.bok/node-state.json（0600）"
fi

# ---------- [4/7] 模型选型 + 下载 ----------
# 档位（口径「配置够上大档、探测建议不强制」）：min=最小可用；a=+纪要档 settle；
# all=+同传档 mt。平台表内 llm_4b/mt/settle 为空=该档未配置：如实告警并按已配置
# 项下载，运行时自动回退（纪要/判据→主模型；同传→主模型）。
pick_models() {
  case "$1" in
    min) SEL_MODELS="asr tts_preset tts_clone llm";;
    a)   SEL_MODELS="asr tts_preset tts_clone llm settle";;
    all) SEL_MODELS="asr tts_preset tts_clone llm settle mt";;
    *)   SEL_MODELS="${1//,/ }";;
  esac
}
suggest_preset() {
  # VRAM 建议：无 GPU/<10GB=min；10–19GB=a；≥20GB=all。
  if (( VRAM_MB == 0 )); then printf 'min'
  elif (( VRAM_MB < 10240 )); then printf 'min'
  elif (( VRAM_MB < 20480 )); then printf 'a'
  else printf 'all'
  fi
}

step "模型选型 + 下载"
if [[ $SKIP_MODELS -eq 1 ]]; then
  note "--skip-models：跳过（假定已在别处下载）"
else
  SUGGEST="$(suggest_preset)"
  if [[ -n "$MODELS_SEL" ]]; then
    pick_models "$MODELS_SEL"
    note "选型（显式）: $MODELS_SEL → $SEL_MODELS"
  elif [[ $ASSUME_YES -eq 1 || ! -t 0 ]]; then
    pick_models "$SUGGEST"
    note "选型（自动，VRAM=${VRAM_MB}MB）: 建议档 $SUGGEST → $SEL_MODELS"
  else
    echo "    建议档: $SUGGEST（VRAM=${VRAM_MB}MB）"
    echo "      min = 最小可用（识别 + 合成 + 主模型）"
    echo "      a   = min + 纪要档（纪要/知识蒸馏；缺则回退主模型）"
    echo "      all = a + 同传档（B 线独立翻译模型；缺则回退主模型）"
    read -r -p "    选择 [min/a/all，回车采纳建议 $SUGGEST]: " ans || true
    pick_models "${ans:-$SUGGEST}"
    note "选型: $SEL_MODELS"
  fi
  run "$PY" "$REPO_ROOT/tools/bok.py" download --only $SEL_MODELS
fi

# ---------- [5/7] systemd 常驻安装 ----------
step "systemd 常驻安装"
# 拓扑 env（经 bok.py Linux 分支透传进单元文件）：媒体监听地址 + 事件回调地址。
BOK_LIVEKIT_BIND="${LIVEKIT_BIND:-$(lan_ip)}"
if [[ -z "$BOK_LIVEKIT_BIND" ]]; then
  BOK_LIVEKIT_BIND="127.0.0.1"
  note "内网 IP 探测失败 —— 媒体监听回落 127.0.0.1（仅本机可用），请以 --bind 显式指定"
fi
export BOK_LIVEKIT_BIND
export BOK_LIVEKIT_WEBHOOK_URL="${WEBHOOK_URL:-$CP_URL/api/webhook/livekit}"
if [[ $INSTALL_SERVICE -eq 0 ]]; then
  note "--no-service：跳过常驻安装（第 6 步以 nohup 前台档拉起）"
elif [[ "$(uname -s)" != "Linux" ]]; then
  note "非 Linux：跳过 systemd（mac 走 launchd 全栈单元 / Windows 走计划任务）"
elif [[ $DRY_RUN -eq 1 ]]; then
  echo "    (dry-run) bok.py prod install --node-agent --cp-url $CP_URL --livekit-url $LIVEKIT_URL --ui-dir $REPO_ROOT/apps/web/out"
  echo "    (dry-run) BOK_LIVEKIT_BIND=$BOK_LIVEKIT_BIND BOK_LIVEKIT_WEBHOOK_URL=$BOK_LIVEKIT_WEBHOOK_URL"
  echo "    (dry-run) root 则装载单元：cp /etc/systemd/system + daemon-reload + enable --now"
else
  unit_args=(--node-agent --cp-url "$CP_URL" --livekit-url "$LIVEKIT_URL"
             --ui-dir "$REPO_ROOT/apps/web/out")
  if [[ -n "$NODE_TOKEN" ]]; then
    unit_args+=(--node-token "$NODE_TOKEN")
  fi
  run "$PY" "$REPO_ROOT/tools/bok.py" prod install "${unit_args[@]}"
  UNIT_SRC="$HOME/.local/share/BokVoice/units/bok-node-agent.service"
  [[ -f "$UNIT_SRC" ]] || UNIT_SRC="$HOME/Library/Application Support/BokVoice/units/bok-node-agent.service"
  if [[ "$(id -u)" -eq 0 && -f "$UNIT_SRC" ]]; then
    cp "$UNIT_SRC" /etc/systemd/system/bok-node-agent.service
    systemctl daemon-reload
    systemctl enable --now bok-node-agent.service
    note "已装载并启动 bok-node-agent.service（崩溃/更新自动拉回；吊销指令保持停止）"
  else
    note "非 root：单元已生成于 $UNIT_SRC —— 以 root 执行："
    note "  cp \"$UNIT_SRC\" /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now bok-node-agent.service"
  fi
fi

# ---------- [6/7] 拉起全栈 + 六点真自检（硬门禁） ----------
step "拉起全栈 + 自检（识别/合成/主模型/媒体/工作进程/控制面）"
selfcheck() {
  local fails="" waited=0
  # 模型加载慢（识别/合成各约 30s，主模型首载可达分钟级）：轮询等待，上限 300s。
  while (( waited < 300 )); do
    fails=""
    curl -fsS --max-time 5 "$CP_URL/health" >/dev/null 2>&1 || fails="$fails 控制面"
    curl -fsS --max-time 5 "http://127.0.0.1:1235/v1/models" >/dev/null 2>&1 || fails="$fails 主模型:1235"
    curl -fsS --max-time 5 "http://127.0.0.1:8787/health" >/dev/null 2>&1 || fails="$fails 识别:8787"
    curl -fsS --max-time 5 "http://127.0.0.1:8788/health" >/dev/null 2>&1 || fails="$fails 合成:8788"
    if ! "$PY" -c '
import socket
for port in (7880, 8081):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
    finally:
        s.close()
' >/dev/null 2>&1; then
      fails="$fails 媒体/工作进程"
    fi
    [[ -z "$fails" ]] && return 0
    sleep 10
    waited=$((waited + 10))
  done
  echo "    !! 自检未通过（等待 ${waited}s）:$fails"
  return 1
}

if [[ $DRY_RUN -eq 1 ]]; then
  note "(dry-run) 轮询六点：控制面 /health、主模型 :1235、识别 :8787、合成 :8788、"
  note "          媒体 :7880、工作进程 :8081"
else
  # 非 root / --no-service：前台拉起全栈（与常驻单元同一条 cmd_up 路径）。
  if [[ $INSTALL_SERVICE -eq 0 || "$(id -u)" -ne 0 ]]; then
    note "前台拉起全栈（nohup node_agent，等效常驻单元路径）"
    BOK_LIVEKIT_BIND="$BOK_LIVEKIT_BIND" BOK_LIVEKIT_WEBHOOK_URL="$BOK_LIVEKIT_WEBHOOK_URL" \
      nohup "$PY" "$REPO_ROOT/tools/node_agent.py" \
      --cp-url "$CP_URL" --livekit-url "$LIVEKIT_URL" \
      --ui-dir "$REPO_ROOT/apps/web/out" --interval 60 >/dev/null 2>&1 &
  fi
  if ! selfcheck; then
    echo "!! [${STEP_NO}/${STEP_TOTAL}] 六点自检未通过 —— 装机判失败" >&2
    echo "   排查: 日志在 \$HOME/.local/share/BokVoice/logs/（asr/tts/llm/livekit/agent）" >&2
    echo "   复检: $PY $REPO_ROOT/tools/bok.py doctor" >&2
    exit 1
  fi
  note "六点自检全部通过"
fi

# ---------- [7/7] 成功摘要（不打印任何凭据） ----------
step "装机完成"
if [[ $DRY_RUN -eq 1 ]]; then
  note "(dry-run) 打印入口 URL 摘要"
else
  LAN="$(lan_ip)"; [[ -n "$LAN" ]] || LAN="127.0.0.1"
  NODE_ID="$("$PY" - <<'PYEOF' 2>/dev/null || true
import json, os
try:
    with open(os.path.expanduser("~/.bok/node-state.json"), encoding="utf-8") as f:
        print(json.load(f).get("node_id", ""))
except Exception:
    print("")
PYEOF
)"
  cat <<EOF

节点装机完成${NODE_ID:+  node=$NODE_ID}    模型档: ${SEL_MODELS:-（跳过下载）}

  话务员入口:   http://$LAN:3000          ← 局域网直连，语音媒体走内网
  管理入口:     $CP_URL                   ← 主管/平台方在云端登录
  健康检查:     $PY $REPO_ROOT/tools/bok.py doctor

  下一步: 平台方在管理入口创建管理员账号 → 配置话术/名册 → 话务员打开本地入口即可接单
EOF
fi
