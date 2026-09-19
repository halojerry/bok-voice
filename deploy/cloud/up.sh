#!/usr/bin/env bash
# deploy/cloud/up.sh —— 云端 CP `docker compose up` 安全包装（重启/重拉唯一入口）。
#
# 为什么必须走本脚本（2026-09-19 bok-cloud-cp-1 crash-loop 事故，restarts=8）：
#   裸 `docker compose up -d cp`（含 --force-recreate）不带 shell 覆盖时，
#   environment 段的 DATABASE_URL 回退 .env 里的 IPv6-only 池器域名
#   （aws-0-ap-southeast-1.pooler.supabase.com）——容器出网无 IPv6，
#   SQLAlchemy 连接失败，容器 crash-loop。同一次事故链还有第二坑：不带
#   BOK_CP_PORT 时宿主口回退 8000，与宿主上的本地开发栈抢 :8000。
#   修复姿势 = 双覆盖（shell env 优先级高于 .env，compose 原生支持）：
#     ① BOK_CP_PORT（shell env > .env > 缺省 18010；本地排练想用 8000 显式设）；
#     ② DATABASE_URL 的 @*.pooler.supabase.com host 段替换为 IPv4（host 已是
#        裸 IP 则原样通过=幂等；aws-0- 前缀变化不影响匹配，只认 *.pooler.
#        supabase.com 后缀）。IP 来源三级（评审 P2-4）：显式 BOK_SUPABASE_IP
#        > 现场解析（getent/dig/nslookup，漂移自动跟随）> 兜底字面量
#        52.77.146.31——池器 IP 会轮换、区域间不同，字面量只是解析全败时
#        的最后防线。
#   裸 up 永远会复现事故——重启/重拉一律走本脚本。任何输出只显示 @host:port
#   掩码形态，凭据绝不回显。详见 docs/DEPLOY_SAAS_RUNBOOK.md「重启/重拉铁律」。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

FALLBACK_SUPABASE_IP="52.77.146.31"

# 池器域名 → IPv4：只动 @ 后的 host 段（[^@/:]+ 不越过凭据/端口/路径），
# 独立函数便于离线自测（与生产路径同一份 sed 逻辑）。
apply_db_override() {
  printf '%s' "$1" | sed -E "s|@[^@/:]+\\.pooler\\.supabase\\.com|@${2:-$FALLBACK_SUPABASE_IP}|"
}

# 现场 A 记录解析（评审 P2-4）：getent（Linux VPS 主力）→ dig → nslookup，
# 全败输出空串，调用方落兜底字面量。只做 A 记录，IPv6 结果一律过滤。
resolve_pooler_ipv4() {  # $1=hostname；stdout=IPv4 或空
  local host="$1"
  if command -v getent >/dev/null 2>&1; then
    getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}'
  elif command -v dig >/dev/null 2>&1; then
    dig +short A "$host" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -n 1
  elif command -v nslookup >/dev/null 2>&1; then
    nslookup -type=A "$host" 2>/dev/null | awk '/^Address/ {print $NF}' \
      | grep -E '^[0-9]+(\.[0-9]+){3}$' | tail -n 1
  fi
}

# 掩码回显：只保留 @host:port 段，用户/密码/库名一律不出现在输出。
mask_db_url() {
  printf '%s' "$1" | sed -E 's|^[^@]*@|@|; s|/.*$||'
}

# ── 解析 DATABASE_URL：环境已设优先（与 compose 优先级一致），否则读同目录 .env
if [[ -z "${DATABASE_URL:-}" ]]; then
  if [[ ! -f "$HERE/.env" ]]; then
    echo "[up.sh] 错误：$HERE/.env 不存在且环境未设 DATABASE_URL——先 cp .env.example .env 或重跑 install.sh" >&2
    exit 1
  fi
  _line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?DATABASE_URL=' "$HERE/.env" | tail -n 1 || true)"
  if [[ -z "${_line:-}" ]]; then
    echo "[up.sh] 错误：$HERE/.env 里没有 DATABASE_URL 行" >&2
    exit 1
  fi
  DATABASE_URL="${_line#*=}"
  DATABASE_URL="${DATABASE_URL%\"}"; DATABASE_URL="${DATABASE_URL#\"}"
  DATABASE_URL="${DATABASE_URL%\'}"; DATABASE_URL="${DATABASE_URL#\'}"
fi
if [[ "$DATABASE_URL" != *'@'* ]]; then
  echo "[up.sh] 错误：DATABASE_URL 缺 @host 段，不像合法连接串（内容不回显）" >&2
  exit 1
fi

# ── 幂等覆盖：pooler 域名 → IPv4（已是 IP 则原样通过）。IP 来源三级：
#    显式 BOK_SUPABASE_IP > 现场 A 记录解析 > 兜底字面量（评审 P2-4）。
_ip=""
if [[ "$DATABASE_URL" == *pooler.supabase.com* ]]; then
  if [[ -n "${BOK_SUPABASE_IP:-}" ]]; then
    _ip="$BOK_SUPABASE_IP"
    echo "[up.sh] 池器 IP 来源=BOK_SUPABASE_IP 显式钉定 $_ip"
  else
    _pooler_host="$(printf '%s' "$DATABASE_URL" | sed -E 's|.*@([^@/:]+).*|\1|')"
    _ip="$(resolve_pooler_ipv4 "$_pooler_host" || true)"
    if [[ -n "$_ip" ]]; then
      echo "[up.sh] 池器 IP 来源=现场解析 $_pooler_host → $_ip"
    else
      _ip="$FALLBACK_SUPABASE_IP"
      echo "[up.sh] 池器 IP 来源=现场解析失败，落兜底字面量 $_ip（IP 漂移时可用 BOK_SUPABASE_IP 钉定）" >&2
    fi
  fi
fi
_db_after="$(apply_db_override "$DATABASE_URL" "$_ip")"
if [[ "$_db_after" == "$DATABASE_URL" ]]; then
  echo "[up.sh] DATABASE_URL host=$(mask_db_url "$DATABASE_URL")（已是 IP/非 pooler 域名，无需覆盖）"
else
  echo "[up.sh] DATABASE_URL host 覆盖：$(mask_db_url "$DATABASE_URL") → $(mask_db_url "$_db_after")"
fi

export DATABASE_URL="$_db_after"

# ── 宿主口解析：shell env 优先（与 compose 优先级一致），否则读 .env，缺省 18010
# （评审修复：原先无视 .env 的显式改口——反代/防火墙钉好的口会被静默改道）。
if [[ -z "${BOK_CP_PORT:-}" ]]; then
  _port_line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?BOK_CP_PORT=' "$HERE/.env" 2>/dev/null | tail -n 1 || true)"
  if [[ -n "${_port_line:-}" ]]; then
    BOK_CP_PORT="${_port_line#*=}"
    BOK_CP_PORT="${BOK_CP_PORT%\"}"; BOK_CP_PORT="${BOK_CP_PORT#\"}"
    BOK_CP_PORT="${BOK_CP_PORT%\'}"; BOK_CP_PORT="${BOK_CP_PORT#\'}"
  fi
fi
export BOK_CP_PORT="${BOK_CP_PORT:-18010}"

echo "[up.sh] docker compose up -d cp $*  （宿主口 ${BOK_CP_PORT} → 容器 8000）"
echo "[up.sh] 起后健康检查：curl -fsS http://127.0.0.1:${BOK_CP_PORT}/health   # 应含 \"ok\":true"

exec docker compose up -d cp "$@"
