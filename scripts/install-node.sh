#!/usr/bin/env bash
# 薄节点部署脚本（spec §11.2；P0=手工档，P2 正式化含离线包 + systemd/launchd 常驻模板）。
# 用法: install-node.sh --cp-url URL --node-token TOK [--repo-root DIR] [--dry-run] [--skip-models]
#
# 行为约定：
#   - 步骤计划器：每步带编号执行，任一步失败立即停机并报告第几步；
#   - --dry-run 只打印计划，零副作用（不建 venv、不下载、不碰 CP、不写 UI 配置）；
#   - [2/6] bootstrap venv；[3/6] Linux CUDA 栈幂等装入 runtime python
#     （基础面包不含 CUDA 栈——GitHub 单资产 2GB 上限；已装即跳过）；
#   - [5/6] 握手探活：单发心跳探针（node_token 自鉴权，心跳端点豁免 CP 门禁）
#     → node_agent --heartbeat-only 后台跑 3s → 查 CP 节点列表确认到达 → 清理。
#     节点列表在 auth-on CP 受 root 门禁保护（401），此时优雅降级——探针 200
#     已是「心跳到达」的权威证据（NodeStore 按 token sha256 命中并更新 last_seen）。
set -euo pipefail

usage() {
  cat <<'EOF'
用法: install-node.sh --cp-url URL (--node-token TOK | --license-key KEY) [--repo-root DIR] [--dry-run] [--skip-models]

  --cp-url       CP 基址（必填，如 http://cp.example.com:8000）
  --node-token   节点令牌（与 --license-key 二选一；由 POST /api/nodes/register 签发，明文只出现一次）
  --license-key  license 流（加固云推荐）：node_agent 自动注册（同机幂等复用
                 node_id），token 落 ~/.bok/node-state.json（0600）重启复用不烧配额
  --repo-root    仓库根目录（缺省自动探测为本脚本上级目录）
  --livekit-url  节点 LiveKit 地址（默认 ws://127.0.0.1:7880；**内网多话务员必填
                 本机内网 IP** 如 ws://192.168.1.10:7880，否则只有节点本机能通话）
  --dry-run      只打印步骤计划，零副作用
  --skip-models  跳过 [3/5] 模型下载（已在别处下载过时用）
EOF
}

CP_URL=""; NODE_TOKEN=""; LICENSE_KEY=""; DRY_RUN=0; SKIP_MODELS=0; REPO_ROOT_ARG=""
LIVEKIT_URL="ws://127.0.0.1:7880"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cp-url) CP_URL="${2:-}"; shift 2;;
    --livekit-url) LIVEKIT_URL="${2:-}"; shift 2;;
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

REPO_ROOT="${REPO_ROOT_ARG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[[ -d "$REPO_ROOT/tools" && -f "$REPO_ROOT/scripts/bootstrap.sh" ]] || {
  echo "repo root 探测失败（缺 tools/ 或 scripts/bootstrap.sh）: $REPO_ROOT" >&2; exit 2;
}
PY="$REPO_ROOT/.venv312/bin/python"

STEP_NO=0
STEP_TOTAL=6
[[ $SKIP_MODELS -eq 1 ]] && STEP_TOTAL=5
AGENT_PID=""

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

# ---------- [1/5] 环境体检（报告性，不阻断） ----------
step "环境体检: GPU/驱动/磁盘"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    (dry-run) 探测 nvidia-smi + df -h"
else
  if command -v nvidia-smi >/dev/null 2>&1; then
    note "NVIDIA GPU:"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader \
      || note "nvidia-smi 查询失败（忽略，不影响安装）"
  else
    note "无 NVIDIA GPU —— mac-mlx/CPU 档继续"
  fi
  note "磁盘（\$HOME 所在卷）: $(df -h "$HOME" | tail -1)"
fi

# ---------- [2/5] Python venv + 依赖 ----------
step "Python venv + 依赖"
run "$REPO_ROOT/scripts/bootstrap.sh"

# ---------- [3/6] CUDA 栈入 runtime python（Linux；幂等跳过） ----------
step "CUDA 依赖装入 runtime python（Linux 基础面包的节点侧补齐）"
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

# ---------- [4/6] 模型下载（--skip-models 可跳） ----------
if [[ $SKIP_MODES -eq 0 ]]; then
  step "模型下载（幂等续传）"
  run "$PY" "$REPO_ROOT/tools/bok.py" download
fi

# ---------- [4/6] 节点握手探活 ----------
step_handshake() {
  if [[ $DRY_RUN -eq 1 ]]; then
    note "(dry-run) ① 单发心跳探针 node_agent.heartbeat_once -> $CP_URL"
    note "(dry-run) ② node_agent --heartbeat-only --interval 1 后台跑 3s"
    note "(dry-run) ③ GET $CP_URL/api/nodes 确认注册表可见后清理"
    return 0
  fi

  # ① 单发心跳探针：与守护进程同一条代码路径（node_agent.heartbeat_once）。
  #   心跳端点对 CP 门禁豁免、node_token 自鉴权——200 即「token 有效 + CP 可达 + 到达」。
  #   license 流没有现成 token（注册由 ② 的 node_agent 完成）→ ① 跳过。
  local rc=0
  if [[ -n "$NODE_TOKEN" ]]; then
    PYTHONPATH="$REPO_ROOT/tools" "$PY" -c '
import sys
from node_agent import NodeConfig, heartbeat_once
ok, body = heartbeat_once(
    NodeConfig(cp_url=sys.argv[1], node_token=sys.argv[2]),
    {"probe": "install-node"},
)
print("[probe] heartbeat -> %s %s" % ("OK" if ok else "FAILED", body))
sys.exit(0 if ok else 1)
' "$CP_URL" "$NODE_TOKEN" || rc=$?
    if (( rc != 0 )); then
      echo "!! [${STEP_NO}/${STEP_TOTAL}] 心跳探针失败：node_token 无效或 CP 不可达（${CP_URL}）" >&2
      exit "$rc"
    fi
  else
    note "license 流：①探针跳过（无现成 token），注册+心跳由 ② node_agent 完成"
  fi

  # ② 后台起 node_agent（1s 一跳，跑 3s ≥2 跳），失败日志落临时文件。
  #   license 流：node_agent 自注册（同机幂等复用 node_id）、token 落状态文件复用。
  local agent_log; agent_log="$(mktemp "${TMPDIR:-/tmp}/bok-node-agent.XXXXXX")"
  local agent_args=(--cp-url "$CP_URL" --livekit-url "$LIVEKIT_URL" \
    --ui-dir "$REPO_ROOT/apps/web/out" --heartbeat-only --interval 1)
  if [[ -n "$NODE_TOKEN" ]]; then
    "$PY" "$REPO_ROOT/tools/node_agent.py" "${agent_args[@]}" \
      --node-token "$NODE_TOKEN" >"$agent_log" 2>&1 &
  else
    # license 经 env 传递（2026-09-16 深测 P3）：argv 在 ps/shell history 可见。
    BOK_LICENSE_KEY="$LICENSE_KEY" "$PY" "$REPO_ROOT/tools/node_agent.py" \
      "${agent_args[@]}" >"$agent_log" 2>&1 &
  fi
  AGENT_PID=$!
  trap cleanup_agent EXIT
  sleep 3

  # ③ CP 端节点列表确认。auth-on CP 该列表走 root 门禁（401）→ 优雅降级，
  #   ①探针 200 已证到达；auth-off（单机缺省）则直接打印注册表佐证。
  rc=0
  local rows
  rows="$("$PY" -c '
import json, sys, urllib.request
url = sys.argv[1].rstrip("/") + "/api/nodes"
try:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as resp:
        rows = json.loads(resp.read().decode() or "[]")
except Exception as exc:
    print("GATED: %s" % exc)
    sys.exit(3)
print(json.dumps(rows, ensure_ascii=False))
' "$CP_URL")" || rc=$?
  if (( rc == 0 )); then
    note "CP 节点注册表："
    "$PY" -c '
import json, sys
for r in json.loads(sys.argv[1]):
    label = r.get("name") or r.get("node_id")
    print("      * %s [%s] last_seen=%s" % (label, r.get("status"), r.get("last_seen_at")))
' "$rows"
  else
    note "节点列表未开放读取（${rows}）——root 门禁保护；心跳到达已由 ① 探针证实"
  fi

  # ④ 清理：杀后台、验日志无心跳失败（3s 内 1s 间隔必有 ≥2 跳，失败必留痕）。
  local agent_pid="$AGENT_PID"
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
  note "node_agent 后台心跳 3s 无失败；runtime-config.js 已注入 $REPO_ROOT/apps/web/out"
}
step "节点握手探活: 心跳探针 → node_agent 后台 → CP 节点列表确认 → 清理"
step_handshake

# ---------- [5/6] doctor 终检（报告性，不阻断——正式版将作硬门禁） ----------
step "doctor 终检（报告性，不阻断）"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    (dry-run) $PY $REPO_ROOT/tools/bok.py doctor"
else
  rc=0
  "$PY" "$REPO_ROOT/tools/bok.py" doctor || rc=$?
  if (( rc != 0 )); then
    note "doctor 退出码 $rc —— 安装完成，建议按 doctor 输出人工复查"
  fi
fi

# ---------- 收尾 ----------
echo
if [[ $DRY_RUN -eq 1 ]]; then
  echo "共 ${STEP_NO}/${STEP_TOTAL} 步（dry-run：未执行任何副作用）。"
else
  echo "共 ${STEP_NO}/${STEP_TOTAL} 步，安装完成。"
  cat <<EOF
下一步（正式常驻部署；systemd/launchd 模板在后续轮提供）:
  1. 常驻心跳 + 全栈:
       nohup $PY $REPO_ROOT/tools/node_agent.py \\
         --cp-url $CP_URL $(if [[ -n "$NODE_TOKEN" ]]; then echo '--node-token ***'; else echo 'BOK_LICENSE_KEY=***（license 经 env 传递，token 已落 ~/.bok/node-state.json 自动复用）'; fi) \\
         --livekit-url $LIVEKIT_URL \\
         --ui-dir $REPO_ROOT/apps/web/out --interval 60 >/dev/null 2>&1 &
     （不带 --heartbeat-only 即拉起全栈 serve + 心跳守护）
  2. 健康观测: 用管理员凭证 GET $CP_URL/api/nodes 确认节点 online
  3. 链路自检: $PY $REPO_ROOT/tools/bok.py doctor
EOF
fi
