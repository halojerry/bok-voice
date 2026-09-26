#!/usr/bin/env bash
# Bok CUDA 部署包 —— 开窗验证清单（docs/CUDA-DEPLOY.md §3 步骤 4 的脚本化）。
# 用法:
#   ./smoke.sh [--e2e] [--skip-node] [--env-file PATH]
#
# 步骤（任何 FAIL 退出码非零；标注「信息位」的步骤只打印不判死）:
#   [1/6] GPU 在场（nvidia-smi）
#   [2/6] sglang A 车道  /v1/models + max_tokens=1 探活 + priority 字段接受性
#   [3/6] sglang judge   /v1/models + max_tokens=1 探活
#   [4/6] radix 前缀命中（信息位：同 prompt 两发测时差）
#   [5/6] priority 竞争 A/B（信息位：饱和请求压住时高优短请求的时延对照）
#   [6/6] bok 节点：单元 active + license/token 状态文件 + 云 CP 可达
#   [--e2e] 追加端到端：scripts/probe_latency_soak.py 真栈一通（需全栈在跑）
#
# 前置：bootstrap.sh 已跑完、env 文件已填、systemd 单元已 enable --now。
# 探针姿势与仓内探针同源（curl 直打 OpenAI 兼容口；max_tokens=1 只量通不量质）。
set -uo pipefail
# 注意：故意不用 set -e——本脚本逐项收集 FAIL，最后统一汇总退出。

ENV_FILE="/etc/bok-cuda/bok-cuda.env"
RUN_E2E=0; SKIP_NODE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --e2e) RUN_E2E=1; shift;;
    --skip-node) SKIP_NODE=1; shift;;
    --env-file) ENV_FILE="${2:-}"; shift 2;;
    -h|--help) sed -n '2,18p' "$0"; exit 0;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done

PASS=0; FAIL=0
ok()   { echo "  PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
info() { echo "  ·     $*"; }

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
else
  echo "[smoke] FAIL: env 文件不存在：${ENV_FILE}（先按 bootstrap.sh [5/5] 指引创建）" >&2
  exit 1
fi
SGLANG_BIND="${SGLANG_BIND:-127.0.0.1}"
SGLANG_PORT_A="${SGLANG_PORT_A:-18100}"
SGLANG_PORT_JUDGE="${SGLANG_PORT_JUDGE:-18101}"
A_BASE="http://127.0.0.1:${SGLANG_PORT_A}"      # 探活恒打回环，不经 SGLANG_BIND
J_BASE="http://127.0.0.1:${SGLANG_PORT_JUDGE}"
AUTH=()
[[ -n "${SGLANG_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${SGLANG_API_KEY}")

curl_code() { # $1=url $2=json-body(可空) → 打印 http code（连接失败=000）
  local code
  if [[ -n "${2:-}" ]]; then
    code="$(curl -sS -o /tmp/bok-smoke-last.json -w '%{http_code}' --max-time 60 \
      -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} -d "$2" "$1" 2>/dev/null || true)"
  else
    code="$(curl -sS -o /tmp/bok-smoke-last.json -w '%{http_code}' --max-time 15 \
      ${AUTH[@]+"${AUTH[@]}"} "$1" 2>/dev/null || true)"
  fi
  echo "${code:-000}"
}

# ================= [1/6] GPU =================
echo; echo "=== [1/6] GPU 在场 ==="
if nvidia-smi >/dev/null 2>&1; then
  ok "nvidia-smi 可用（$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)）"
else
  bad "nvidia-smi 不可用——GPU/driver 出问题，后面全免谈"
fi

# ================= [2/6] A 车道 =================
echo; echo "=== [2/6] sglang A 车道 :${SGLANG_PORT_A}（a_reply） ==="
CODE="$(curl_code "$A_BASE/v1/models")"
if [[ "$CODE" == "200" ]]; then
  ok "GET /v1/models → 200"
else
  bad "GET /v1/models → ${CODE}（单元没起 / 端口不对 / 配了 --api-key 而本脚本缺 key）。journalctl -u sglang-a -n50 排查"
fi
CODE="$(curl_code "$A_BASE/v1/chat/completions" \
  "{\"model\":\"${MODEL_A_REPLY_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"回复ok两个字母\"}],\"max_tokens\":1}")"
if [[ "$CODE" == "200" ]]; then
  ok "chat max_tokens=1 → 200（模型 ${MODEL_A_REPLY_NAME:-?} 探活通过）"
else
  bad "chat max_tokens=1 → ${CODE}：模型目录/served-model-name/env 三处对一下（见 /tmp/bok-smoke-last.json）"
fi
# priority 字段接受性：--enable-priority-scheduling 已开 → 200 且字段生效；
# 万一单元旗标被改丢，该字段会被静默忽略——仍 200，所以这里只验「不 4xx」。
CODE="$(curl_code "$A_BASE/v1/chat/completions" \
  "{\"model\":\"${MODEL_A_REPLY_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1,\"priority\":5}")"
if [[ "$CODE" == "200" ]]; then
  ok "带 priority 字段的请求 → 200（priority 调度链路在场；A/B 对照见 [5/6]）"
else
  bad "带 priority 字段的请求 → ${CODE}（4xx=服务端拒绝了 priority 字段，核对 --enable-priority-scheduling 旗标）"
fi

# ================= [3/6] judge 车道 =================
echo; echo "=== [3/6] sglang judge 车道 :${SGLANG_PORT_JUDGE}（judge/settle） ==="
CODE="$(curl_code "$J_BASE/v1/models")"
if [[ "$CODE" == "200" ]]; then
  ok "GET /v1/models → 200"
else
  bad "GET /v1/models → ${CODE}（journalctl -u sglang-judge -n50 排查）"
fi
CODE="$(curl_code "$J_BASE/v1/chat/completions" \
  "{\"model\":\"${MODEL_JUDGE_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"回复ok两个字母\"}],\"max_tokens\":1}")"
if [[ "$CODE" == "200" ]]; then
  ok "chat max_tokens=1 → 200（模型 ${MODEL_JUDGE_NAME:-?} 探活通过）"
else
  bad "chat max_tokens=1 → ${CODE}（见 /tmp/bok-smoke-last.json）"
fi

# ================= [4/6] radix 命中（信息位） =================
echo; echo "=== [4/6] radix 前缀命中（信息位，不判死） ==="
PREFIX="You are a customer service agent. The following policy is long and static: $(head -c 2000 /dev/zero | tr '\0' 'x')"
radix_time() {
  curl -sS -o /dev/null --max-time 60 -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} \
    -d "{\"model\":\"${MODEL_A_REPLY_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"$PREFIX\"}],\"max_tokens\":1}" \
    -w '%{time_total}' "$A_BASE/v1/chat/completions"
}
T1="$(radix_time)"; T2="$(radix_time)"
if [[ -n "$T1" && -n "$T2" ]]; then
  info "同 prompt 两发：第1发 ${T1}s / 第2发 ${T2}s ——第2发明显更快=radix 命中在工作"
  awk -v a="$T1" -v b="$T2" 'BEGIN{exit !(b < a)}' \
    && info "结论：命中趋势正常" \
    || info "结论：未见加速（可能 KV 池太小/模型太慢/首发即全命中，开窗时看 sglang 日志 cached_tokens）"
else
  info "测时失败（车道不通时本步骤自然跳过）"
fi

# ================= [5/6] priority 竞争 A/B（信息位） =================
echo; echo "=== [5/6] priority 竞争 A/B（信息位，不判死） ==="
# 姿势：一发长生成压住车道，0.5s 后插一发 max_tokens=1 的高优请求，量其时延；
# 与无竞争基线对照。注意 agent 侧「每请求带 priority」的接线是开窗待办
# （docs/CUDA-DEPLOY.md §8），当前全体请求默认 priority=0 → 本探针只证明
# 「字段通道通」，不证明「真实回复已享受高优」。
BASELINE="$(curl -sS -o /dev/null --max-time 60 -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} \
  -d "{\"model\":\"${MODEL_A_REPLY_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" \
  -w '%{time_total}' "$A_BASE/v1/chat/completions")"
curl -sS -o /dev/null --max-time 120 -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} \
  -d "{\"model\":\"${MODEL_A_REPLY_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"写一段500字关于快递的故事\"}],\"max_tokens\":400}" \
  "$A_BASE/v1/chat/completions" &
SAT_PID=$!
sleep 0.5
CONTENDED="$(curl -sS -o /dev/null --max-time 120 -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} \
  -d "{\"model\":\"${MODEL_A_REPLY_NAME:-}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1,\"priority\":100}" \
  -w '%{time_total}' "$A_BASE/v1/chat/completions")"
wait "$SAT_PID" 2>/dev/null || true
info "基线（无竞争）${BASELINE:-?}s / 饱和压制中高优请求 ${CONTENDED:-?}s"
info "判读：contended 接近 baseline=priority 生效趋势；远大于 baseline=排队仍在让路（开窗时抓 sglang 日志逐请求 priority 核对）"

# ================= [6/6] bok 节点 =================
echo; echo "=== [6/6] bok 节点注册与心跳 ==="
if (( SKIP_NODE == 1 )); then
  info "--skip-node：跳过"
else
  if systemctl is-active --quiet bok-node 2>/dev/null; then
    ok "systemd 单元 bok-node active"
  else
    bad "systemd 单元 bok-node 非 active（journalctl -u bok-node -n50 排查；常见=env 缺 BOK_CP_URL/凭据、\$BOK_REPO_ROOT 无 node_agent.py）"
  fi
  if [[ -f "$HOME/.bok/node-state.json" ]]; then
    ok "license 流 token 状态文件在场：~/.bok/node-state.json（0600）"
  else
    info "无 node-state.json——token 直传模式（BOK_NODE_TOKEN）属正常；license 模式缺它=注册没成"
  fi
  CP_HEALTH="${BOK_CP_URL%/}/health"
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$CP_HEALTH" 2>/dev/null || echo 000)"
  if [[ "$CODE" == "200" ]]; then
    ok "云 CP 可达：$CP_HEALTH → 200"
  else
    bad "云 CP 不可达：$CP_HEALTH → ${CODE}（BOK_CP_URL 填错/网络不通/CP 没起）"
  fi
  # 心跳证据：journal 里近 5 分钟的心跳日志（node_agent 每 60s 一跳）。
  if journalctl -u bok-node --since "-5 min" 2>/dev/null | grep -qi "heartbeat\|node"; then
    ok "近 5 分钟有节点日志（注册/心跳链路有动静；云台 nodes 页应能看到本节点）"
  else
    info "近 5 分钟无节点日志——单元刚装/心跳周期未到时属正常，等 60s 复看"
  fi
fi

# ================= [--e2e] 端到端一通 =================
if (( RUN_E2E == 1 )); then
  echo; echo "=== [e2e] probe_latency_soak（真栈一通测试通话） ==="
  REPO_ROOT="${BOK_REPO_ROOT:-$HOME/bok-voice}"
  E2E_PY="$REPO_ROOT/.venv312/bin/python"
  if [[ ! -x "$E2E_PY" ]]; then
    bad "找不到 ${E2E_PY}——e2e 需要完整仓库+仓内 venv（scripts/bootstrap.sh），且 CP/LiveKit/ASR/TTS/worker 全栈在跑"
  else
    info "跑 probe_latency_soak.py --scenario soak-canto（CONTROL_PLANE_URL=${CONTROL_PLANE_URL:-http://127.0.0.1:8000}）"
    ( cd "$REPO_ROOT" \
      && CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-http://127.0.0.1:8000}" \
         TTS_URL="${TTS_URL:-http://127.0.0.1:8788}" \
         BOK_CP_TOKEN="${BOK_CP_TOKEN:-}" \
         "$E2E_PY" scripts/probe_latency_soak.py --scenario soak-canto \
         --budget-perceived-ms "${BOK_E2E_BUDGET_PERCEIVED_MS:-3000}" )
    if (( $? == 0 )); then ok "端到端探针 PASS"; else bad "端到端探针 FAIL（读上方报告：哑轮/PERCEIVED 超预算/异常旗）"; fi
  fi
fi

# ================= 汇总 =================
echo
echo "=== 汇总：PASS=$PASS FAIL=$FAIL ==="
(( FAIL == 0 )) || echo "[smoke] 结论：FAIL——按上面逐条排查后重跑；排坑对照 docs/CUDA-DEPLOY.md"
(( FAIL == 0 )) && echo "[smoke] 结论：OK——可进入云 CP 路由接入与验收判据表（docs/CUDA-DEPLOY.md §5-§6）"
exit $(( FAIL > 0 ? 1 : 0 ))
