#!/usr/bin/env bash
# =============================================================================
# Bok Voice · 电话边缘站点部署 runbook（VPS 单租户档，spec 2026-09-13 P1.5 §7）
# =============================================================================
# 目标机：Ubuntu 22.04+（Debian 系可用），root 或 sudo。把一台公网 VPS 变成
# 「电话边缘站点」——Redis + livekit-sip（上游源码原生编译）+ systemd 常驻：
#
#   1. apt 依赖：redis-server + Go(>=1.21) + livekit-sip 构建/运行库
#   2. /opt/livekit-sip       上游源码 clone（幂等：已有则 fetch 更新）
#   3. /usr/local/bin/livekit-sip    mage build 产物（--force 强制重编）
#   4. /etc/bok/livekit-sip.yaml     0640 root:livekit-sip（含 API 密钥，勿入 git）
#   5. /etc/systemd/system/bok-livekit-sip.service   Restart=always
#   6. 结尾打印防火墙提示（只打印不代开）与健康检查命令
#
# 幂等语义：二进制在 + 源码目录已克隆 → 跳过编译（--force 重编）；
#          配置文件内容有变才覆盖（原文件先备份为 *.bak.<时间戳>）。
#
# 配置键名依据（2026-09-15 逐条核实，改前先复核上游源码）：
#   - livekit/sip pkg/config/config.go：api_key / api_secret / ws_url /
#     redis.address / sip_port / rtp_port / use_external_ip / logging.level。
#   - redis.address ← livekit/protocol redis.RedisConfig（同结构含 username/
#     password/db）；logging.level ← livekit/protocol logger.Config（README 里的
#     log_level 是旧写法：结构体无该字段，yaml 静默忽略，勿抄）。
#   - rtp_port 是 rtcconfig.PortRange，其 UnmarshalYAML 只认字符串形态
#     "10000-20000"；写成 start:/end: 映射会被静默忽略（源码实证）。
#   - 构建依赖（README + build/sip/Dockerfile 构建层）：pkg-config
#     libopus-dev libopusfile-dev libsoxr-dev **+ build-essential**（cgo 链路：
#     media-sdk → amrwb-cgo 的 dec/enc 全靠 #cgo，无 C 编译器必失败）；产物动态
#     链接运行库 libopus0 libopusfile0 libsoxr0（Dockerfile 运行层同款清单）。
#   - 端口面（docs.livekit.io/transport/self-hosting/sip-server）：5060/UDP
#     SIP 信令 + 10000-20000/UDP RTP 媒体必须公网可达。
#
# 用法：
#   sudo scripts/deploy_sip_edge.sh \
#     --api-key <站点 LiveKit API key> --api-secret <站点 LiveKit API secret> \
#     --livekit-url ws://127.0.0.1:7880 [--redis-url redis://127.0.0.1:6379] [--force]
#
# 注意：--api-secret 以明文进 argv（本机 ps 可见，引导期一次性动作）；密钥落在
#       /etc/bok/livekit-sip.yaml（0640 root:livekit-sip），不要提交进仓库。
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/livekit/sip"
SRC_DIR="/opt/livekit-sip"
BIN_PATH="/usr/local/bin/livekit-sip"
CONF_DIR="/etc/bok"
CONF_FILE="${CONF_DIR}/livekit-sip.yaml"
UNIT_NAME="bok-livekit-sip"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"
SERVICE_USER="livekit-sip"
SIP_PORT="5060"
RTP_START="10000"
RTP_END="20000"
GO_MIN="1.21"          # Go 工具链自动下载（GOTOOLCHAIN=auto）的最低版本
MAGE_PKG="github.com/magefile/mage@latest"

# 构建依赖（README「Running locally」+ build/sip/Dockerfile 构建层）：
#   · pkg-config libopus-dev libopusfile-dev libsoxr-dev（README 明列）
#   · build-essential 是 README 没写的**必须项**：上游 go.mod 经 media-sdk →
#     livekit/amrwb-cgo 走 cgo（dec/enc 包全靠 #cgo），无 C 编译器时
#     CGO_ENABLED=0 → "build constraints exclude all Go files" 编译失败
#     （Docker 里没暴露这个坑：官方 golang 基础镜像自带 gcc；2026-09-15 容器实证）。
# 运行库（Dockerfile 运行层）：libopus0 libopusfile0 libsoxr0 ca-certificates。
APT_PKGS_BASE=(ca-certificates curl git iproute2 redis-server)
APT_PKGS_BUILD=(pkg-config build-essential libopus-dev libopusfile-dev libsoxr-dev)
APT_PKGS_RUNTIME=(libopus0 libopusfile0 libsoxr0)

API_KEY=""
API_SECRET=""
LIVEKIT_URL=""
REDIS_URL=""
FORCE=0

# ---- 输出小工具 --------------------------------------------------------------

log()  { printf '[deploy-sip-edge] %s\n' "$*"; }
warn() { printf '[deploy-sip-edge] WARN: %s\n' "$*" >&2; }
die()  { printf '[deploy-sip-edge] ERROR: %s\n' "$*" >&2; exit 1; }

step() { printf '\n[deploy-sip-edge] === %s ===\n' "$*"; }

usage() {
  cat <<'USAGE'
Bok Voice 电话边缘站点部署（Ubuntu 22.04+，root 或 sudo）

用法：
  sudo scripts/deploy_sip_edge.sh --api-key K --api-secret S --livekit-url URL [选项]

必填：
  --api-key KEY        站点 LiveKit 的 API key（与 --livekit-url 同一部署）
  --api-secret SECRET  站点 LiveKit 的 API secret（含密钥，勿提交仓库）
  --livekit-url URL    站点 LiveKit 地址，ws:// 或 wss://（同机=ws://127.0.0.1:7880）

可选：
  --redis-url URL      Redis 地址；默认 redis://127.0.0.1:6379。
                       形态：redis://[user:pass@]host[:port][/db]，也接受裸 host:port。
                       **必须与该站点 LiveKit 用的是同一个 Redis**（psrpc 耦合硬依赖）。
  --force              重编 livekit-sip（默认：二进制在则跳过编译）
  -h, --help           本帮助
USAGE
}

# ---- 参数解析（先于 sudo 自重启：--help 与参数错不用提权即可看到） ----------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key|--api-secret|--livekit-url|--redis-url)
      [[ $# -ge 2 ]] || die "参数 $1 缺值"
      case "$1" in
        --api-key)     API_KEY="$2" ;;
        --api-secret)  API_SECRET="$2" ;;
        --livekit-url) LIVEKIT_URL="$2" ;;
        --redis-url)   REDIS_URL="$2" ;;
      esac
      shift 2
      ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 看用法）" ;;
  esac
done

[[ -n "${API_KEY}" ]]     || die "--api-key 必填（站点 LiveKit 的 API key）"
[[ -n "${API_SECRET}" ]]  || die "--api-secret 必填（站点 LiveKit 的 API secret）"
[[ -n "${LIVEKIT_URL}" ]] || die "--livekit-url 必填（站点 LiveKit 的 ws:// / wss:// 地址）"
case "${LIVEKIT_URL}" in
  ws://*|wss://*) : ;;
  *) die "--livekit-url 必须 ws:// 或 wss:// 开头（收到：${LIVEKIT_URL}）" ;;
esac
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379}"

# ---- Redis URL 解析（→ REDIS_ADDR/REDIS_USER/REDIS_PASS/REDIS_DB） -----------

REDIS_ADDR=""
REDIS_USER=""
REDIS_PASS=""
REDIS_DB="0"

parse_redis_url() {
  local raw="$1" rest="" cred="" hostport="" host="" port="6379" db=""
  case "${raw}" in
    rediss://*)
      die "本 runbook 只覆盖同机明文 Redis（redis://）。rediss:// 需自备 TLS 配置（redis.tls）——请手工部署后把 systemd 指到自管 config"
      ;;
    redis://*) rest="${raw#redis://}" ;;
    *://*)     die "无法识别的 --redis-url：${raw}（支持 redis:// 或裸 host:port）" ;;
    *)         rest="${raw}" ;;
  esac

  # [user[:pass]@]host[:port][/db]
  if [[ "${rest}" == *"@"* ]]; then
    cred="${rest%%@*}"
    rest="${rest#*@}"
    if [[ "${cred}" == *:* ]]; then
      REDIS_USER="${cred%%:*}"
      REDIS_PASS="${cred#*:}"
    else
      REDIS_PASS="${cred}"
    fi
  fi
  if [[ "${rest}" == *"/"* ]]; then
    db="${rest#*/}"
    rest="${rest%%/*}"
  fi
  hostport="${rest}"
  if [[ "${hostport}" == *:* ]]; then
    host="${hostport%%:*}"
    port="${hostport##*:}"
  else
    host="${hostport}"
  fi

  [[ "${host}" =~ ^[A-Za-z0-9._-]+$ ]] || die "非法 Redis 主机名：${host}"
  [[ "${port}" =~ ^[0-9]+$ ]]         || die "非法 Redis 端口：${port}"
  [[ -z "${db}" || "${db}" =~ ^[0-9]+$ ]] || die "非法 Redis db：${db}"

  REDIS_ADDR="${host}:${port}"
  REDIS_DB="${db:-0}"
}
parse_redis_url "${REDIS_URL}"

# ---- 前置：root（非 root 经 sudo 自重启；参数校验已过，提权只为写系统） --------

if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SELF_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    log "需要 root——经 sudo 重新执行 ${SELF_PATH}"
    exec sudo -- bash "${SELF_PATH}" "$@"
  fi
  die "需要 root：请用 sudo 运行本脚本（或先安装 sudo）"
fi

# ---- 版本比较（sort -V） -----------------------------------------------------

version_ge() {  # $1 >= $2 ?
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

go_bin() { command -v go 2>/dev/null || true; }
go_cur_version() { "${GO_BIN}" version 2>/dev/null | sed -E 's/^go version go([0-9]+\.[0-9]+(\.[0-9]+)?).*/\1/'; }

# ---- YAML 值转义（双引号标量） ----------------------------------------------

yaml_str() {
  local s="$1"
  [[ "${s}" != *$'\n'* && "${s}" != *$'\r'* ]] || die "配置值不能含换行"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "${s}"
}

# ---- 步骤 1：系统与依赖 ------------------------------------------------------

step "1/6 系统检查与 apt 依赖"

[[ -r /etc/os-release ]] || die "读不到 /etc/os-release——本脚本只支持 Debian 系（Ubuntu 22.04+）"
# shellcheck disable=SC1091
. /etc/os-release
log "系统：${PRETTY_NAME:-unknown}"
case "${ID:-}" in
  ubuntu)
    if ! version_ge "${VERSION_ID:-0}" "22.04"; then
      warn "目标档 Ubuntu 22.04+（当前 ${VERSION_ID:-?}）——继续，但旧版本的 apt Go 可能 <1.21"
    fi
    ;;
  debian) warn "Debian 系兼容运行（目标档是 Ubuntu 22.04+）" ;;
  *)      warn "${ID:-unknown} 非 Debian 系：apt 步骤可能失败，继续尝试" ;;
esac
command -v apt-get >/dev/null 2>&1 || die "找不到 apt-get——本 runbook 只覆盖 Debian 系"

export DEBIAN_FRONTEND=noninteractive
log "apt-get update（可能耗时）"
apt-get update -y -qq
log "安装：${APT_PKGS_BASE[*]} ${APT_PKGS_BUILD[*]} ${APT_PKGS_RUNTIME[*]}"
apt-get install -y -qq --no-install-recommends \
  "${APT_PKGS_BASE[@]}" "${APT_PKGS_BUILD[@]}" "${APT_PKGS_RUNTIME[@]}"

# Go：apt 的 golang-go（22.04=1.18 / 24.04=1.22）——不足 1.21 时给人工安装提示。
GO_BIN="$(go_bin)"
if [[ -z "${GO_BIN}" ]]; then
  log "安装 golang-go（apt 版 Go）"
  apt-get install -y -qq --no-install-recommends golang-go
  GO_BIN="$(go_bin)"
fi
[[ -n "${GO_BIN}" ]] || die "go 安装失败——请手工安装 Go >= ${GO_MIN}（https://go.dev/dl/）后重跑"
GO_CUR="$(go_cur_version)"
[[ -n "${GO_CUR}" ]] || die "无法解析 go version 输出（GO_BIN=${GO_BIN}）"
if ! version_ge "${GO_CUR}" "${GO_MIN}"; then
  die "$(cat <<EOF
Go ${GO_CUR} < ${GO_MIN}（apt 的 golang-go 太旧——Ubuntu 22.04 只给 1.18）。
请装新版 Go 后重跑，二选一：
  snap install go --classic
  curl -fsSL -o /tmp/go.tgz https://go.dev/dl/<最新版>.linux-amd64.tar.gz \\
    && rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz \\
    && export PATH=/usr/local/go/bin:\$PATH   # 写进 /etc/profile.d/ 或本命令同 shell 续跑
EOF
)"
fi
log "Go ${GO_CUR}（>= ${GO_MIN}，OK）"

# Redis 常驻（apt 包装完即启动；显式 enable 保证重启后自起）
systemctl enable --now redis-server >/dev/null 2>&1 || warn "redis-server enable 失败——请手工 systemctl enable --now redis-server"
if [[ "${REDIS_ADDR}" == "127.0.0.1:6379" || "${REDIS_ADDR}" == "localhost:6379" ]]; then
  if command -v redis-cli >/dev/null 2>&1; then
    if [[ "$(redis-cli -h 127.0.0.1 ping 2>/dev/null || true)" == "PONG" ]]; then
      log "本机 Redis PONG"
    else
      warn "本机 Redis 未应答 PONG——livekit-sip 起不来会刷连接错误，先查 systemctl status redis-server"
    fi
  fi
fi
log "Redis：${REDIS_ADDR}（db=${REDIS_DB}）——必须与站点 LiveKit 的 Redis 同一个（psrpc 耦合）"

# ---- 步骤 2：编译 livekit-sip ------------------------------------------------

step "2/6 编译 livekit-sip → ${BIN_PATH}"

if [[ "${FORCE}" -eq 0 && -x "${BIN_PATH}" ]]; then
  log "已装 ${BIN_PATH}（--force 可重编）——跳过编译"
else
  if [[ -d "${SRC_DIR}/.git" ]]; then
    log "更新源码 ${SRC_DIR}"
    git -C "${SRC_DIR}" fetch --depth 1 origin HEAD
    git -C "${SRC_DIR}" reset --hard FETCH_HEAD
  elif [[ -d "${SRC_DIR}" ]]; then
    [[ "${FORCE}" -eq 1 ]] || die "${SRC_DIR} 已存在且不是 git 仓库——清掉它或加 --force 重跑"
    log "--force：清掉非 git 的 ${SRC_DIR}"
    rm -rf "${SRC_DIR}"
  fi
  if [[ ! -d "${SRC_DIR}/.git" ]]; then
    log "clone ${REPO_URL} → ${SRC_DIR}"
    git clone --depth 1 "${REPO_URL}" "${SRC_DIR}"
  fi

  # Go 工具链：go.mod 要求的版本可能高于本机（上游 main 追得很新）。
  export GOPATH="${GOPATH:-$("${GO_BIN}" env GOPATH)}"
  export PATH="${PATH}:${GOPATH}/bin:/usr/local/go/bin"
  GO_REQ="$(awk '/^go [0-9]/{print $2; exit}' "${SRC_DIR}/go.mod" 2>/dev/null || true)"
  if [[ -n "${GO_REQ}" ]] && ! version_ge "${GO_CUR}" "${GO_REQ}"; then
    log "本机 Go ${GO_CUR} < 上游 go.mod 要求 ${GO_REQ}——交给 GOTOOLCHAIN=auto 自动下载工具链（需外网）"
    export GOTOOLCHAIN=auto
  fi

  if ! command -v mage >/dev/null 2>&1; then
    log "安装 mage（${MAGE_PKG}）"
    "${GO_BIN}" install "${MAGE_PKG}"
  fi
  command -v mage >/dev/null 2>&1 || die "mage 安装失败——PATH 里找不到 mage（GOPATH/bin=${GOPATH}/bin）"

  log "mage build（上游是 go build -a 全量重编，首次可能 5-15 分钟）"
  # cgo 必开：amrwb-cgo 的 dec/enc 全靠 #cgo（上游 Dockerfile 同样显式 CGO_ENABLED=1）
  export CGO_ENABLED=1
  ( cd "${SRC_DIR}" && mage build )

  BUILT=""
  for cand in "${GOPATH}/bin/sip" "${SRC_DIR}/bin/sip"; do
    if [[ -x "${cand}" ]]; then BUILT="${cand}"; break; fi
  done
  [[ -n "${BUILT}" ]] || die "编译产物没找到（期望 ${GOPATH}/bin/sip 或 ${SRC_DIR}/bin/sip）"
  log "安装产物：${BUILT} → ${BIN_PATH}"
  install -m 0755 -o root -g root "${BUILT}" "${BIN_PATH}"
fi
log "二进制就位：${BIN_PATH}"

# ---- 步骤 3：服务用户与配置 --------------------------------------------------

step "3/6 渲染 ${CONF_FILE}"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  log "创建系统用户 ${SERVICE_USER}"
  useradd --system --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin --comment "Bok Voice SIP edge" "${SERVICE_USER}"
fi
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONF_DIR}"

TMP_CONF="$(mktemp)"
trap 'rm -f "${TMP_CONF:-}"' EXIT
{
  cat <<'YAML_HEAD'
# Bok Voice · 电话边缘站点 livekit-sip 配置
# 由 scripts/deploy_sip_edge.sh 渲染（重跑覆盖前自动备份 *.bak.<时间戳>）。
# 键名依据 livekit/sip pkg/config/config.go + protocol 的 redis/logger 配置结构。
YAML_HEAD
  printf 'api_key: %s\n' "$(yaml_str "${API_KEY}")"
  printf 'api_secret: %s\n' "$(yaml_str "${API_SECRET}")"
  printf 'ws_url: %s\n' "$(yaml_str "${LIVEKIT_URL}")"
  printf 'redis:\n'
  printf '  address: %s\n' "$(yaml_str "${REDIS_ADDR}")"
  if [[ -n "${REDIS_USER}" ]]; then printf '  username: %s\n' "$(yaml_str "${REDIS_USER}")"; fi
  if [[ -n "${REDIS_PASS}" ]]; then printf '  password: %s\n' "$(yaml_str "${REDIS_PASS}")"; fi
  if [[ "${REDIS_DB}" != "0" ]]; then printf '  db: %s\n' "${REDIS_DB}"; fi
  cat <<YAML_TAIL

sip_port: ${SIP_PORT}
# RTP 媒体端口段——rtcconfig.PortRange 只认 "start-end" 字符串形态
# （写成 start:/end: 映射会被静默忽略），且必须与防火墙/云安全组放行段一致。
rtp_port: ${RTP_START}-${RTP_END}
# 公网 IP 自动发现：SDP 里通告公网地址（公网 VPS 必开，否则对端无音频）
use_external_ip: true
logging:
  level: info
YAML_TAIL
} > "${TMP_CONF}"

if [[ -f "${CONF_FILE}" ]] && ! cmp -s "${TMP_CONF}" "${CONF_FILE}"; then
  BAK="${CONF_FILE}.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "${CONF_FILE}" "${BAK}"
  log "原配置已备份：${BAK}"
fi
install -m 0640 -o root -g "${SERVICE_USER}" "${TMP_CONF}" "${CONF_FILE}"
log "写入 ${CONF_FILE}（0640 root:${SERVICE_USER}）"

# ---- 步骤 4：systemd 单元 ----------------------------------------------------

step "4/6 安装 systemd 单元 ${UNIT_NAME}.service"

# Redis 依赖行按 --redis-url 分支（T6 审查遗留）：
#   本机档（127.0.0.1:6379 / localhost:6379）= livekit-sip 与 Redis 同机，Redis
#   停则 SIP 必死——Requires 强依赖（redis 重启连带拉起 SIP，避免断连空转）。
#   远端档 = 站点 Redis 在别处（形态 2 里 SIP 与站点 LiveKit 可不同机），本机
#   apt 装上的 redis-server 只是旁路：用 Wants 弱依赖，别让本机 redis 的状态
#   拖停/拖起重启真正在用的远端连路。
if [[ "${REDIS_ADDR}" == "127.0.0.1:6379" || "${REDIS_ADDR}" == "localhost:6379" ]]; then
  REDIS_UNIT_DEP="# 本机 Redis（同机档）：强依赖——redis 停则 SIP 停
Requires=redis-server.service"
else
  REDIS_UNIT_DEP="# 远端 Redis ${REDIS_ADDR}：本机 redis-server 与站点无关，弱依赖
Wants=redis-server.service"
fi

cat > "${UNIT_FILE}" <<UNIT
[Unit]
Description=Bok Voice 电话边缘 SIP（livekit-sip）
Documentation=https://github.com/livekit/sip
After=network-online.target redis-server.service
Wants=network-online.target
${REDIS_UNIT_DEP}

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
ExecStart=${BIN_PATH} --config=${CONF_FILE}
Restart=always
RestartSec=3
# RTP 端口段与 5060 均 >1024：非特权用户即可绑定，无需 root/能力位
NoNewPrivileges=true
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT
log "写入 ${UNIT_FILE}"

# ---- 步骤 5：启用并启动 ------------------------------------------------------

step "5/6 daemon-reload + enable + start"

systemctl daemon-reload
systemctl enable "${UNIT_NAME}" >/dev/null
systemctl restart "${UNIT_NAME}"
sleep 2
if systemctl is-active --quiet "${UNIT_NAME}"; then
  log "服务 active"
else
  warn "服务未 active——journalctl -u ${UNIT_NAME} -n 50 --no-pager 看原因"
fi

# ---- 步骤 6：收尾提示（防火墙只打印不代开） ---------------------------------

step "6/6 后续动作（防火墙/安全组需人工放行）"

PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
SIP_URI="${PUBLIC_IP:-<本机公网IP>}:${SIP_PORT}"
WORKER_LIVEKIT_URL="wss://${PUBLIC_IP:-<vps>}"

cat <<CHECK

[安全组/防火墙] 必须放行以下 UDP（脚本不代开——云安全组在控制台配）：
  · 5060/UDP          SIP 信令（服务同时监听 5060/TCP，对端走 TCP transport 才需要放行）
  · ${RTP_START}-${RTP_END}/UDP   RTP 媒体
  本机 ufw 参考：ufw allow 5060/udp && ufw allow ${RTP_START}:${RTP_END}/udp

[健康检查]
  systemctl status ${UNIT_NAME} --no-pager
  ss -ulnp | grep -E ':${SIP_PORT}\b'        # 无 ss 时用：lsof -i :${SIP_PORT}
  journalctl -u ${UNIT_NAME} -n 50 --no-pager

[站点 SIP URI] ${SIP_URI}
  面板注册 trunk 时 address 填 trunk 商给的地址；此 URI 是本站点对外信令面。

[下一步]
  1) CP 登记站点：当前 CP 只有 GET /api/sip/sites 与
     POST /api/sip/sites/{id}/trunk（站点建行入口待补）——livekit_url 填站点
     LiveKit 地址、sip_edge 填 cloud、numbers 填主叫号池。
  2) 面板「设置 → 外呼（SIP）」切 real 档 → 选站点 → 填 trunk 商地址/主叫号/
     鉴权 → 「注册 trunk」（成功后 trunk_id 自动回填，记得保存设置）。
  3) 战役挂站点：POST /api/campaigns 带 site_id——dial 块 trunk 按站点优先取。
  4) Mac 侧 GPU worker 出站注册到本站点（无任何入站端口需求）：
       LIVEKIT_URL=${WORKER_LIVEKIT_URL} \\
       LIVEKIT_API_KEY=<站点 key> LIVEKIT_API_SECRET=<站点 secret> \\
       python tools/bok.py serve
     （worker 只出站连 LiveKit；CP 的 LIVEKIT_URL/凭据同源才能签 token/dispatch）
  5) 真中继启用前置门（spec §6，2026-09-15 审查定案）：8kHz 窄带 mock 档已过，
     仍需闭环「带前缀粤语报号窄带复测」+「真 G.711 样本回填」两项，缺一不放行。

[提醒] 本机 LiveKit 站点必须与 livekit-sip 共用同一个 Redis（psrpc 硬依赖）：
  站点 LiveKit 若起在本机，其配置里的 redis 地址要与 ${REDIS_ADDR} 一致。

CHECK
log "完成。"
