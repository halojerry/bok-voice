#!/usr/bin/env bash
# 云端控制面一键部署（thin-node SaaS 云侧）——宝塔/任意 Docker 主机通用。
#
# 干什么：前置检查 → 生成 .env（密钥自动随机，绝不覆盖既有文件）→ docker compose up
#        → 轮询健康 → 三项验收（health/静态站豁免/root 登录）→ 打印节点装机一条龙。
#
# 用法（在本目录或任意位置执行均可）：
#   ./install.sh --database-url 'postgresql+psycopg://postgres.REF:PWD@aws-0-REGION.pooler.supabase.com:5432/postgres'
#
#   全参数（都有环境变量等价物）：
#     --database-url URL   Supabase session pooler 连接串（必填；或 env DATABASE_URL）
#     --jwt-secret S       JWT 密钥（缺省自动 openssl rand -hex 32）
#     --cp-token S         机器通道 token（缺省自动 openssl rand -hex 24；agent worker 需同值）
#     --root-user NAME     root 用户名（默认 admin）
#     --root-pass S        root 密码（缺省自动生成并打印一次）
#     --port N             宿主端口（默认 8000；或 env BOK_CP_PORT）
#     --image REF          镜像引用（默认 ghcr.io/halojerry/bok-voice:latest，可钉 v0.2.0）
#     --dry-run            只生成/校验配置，不起容器
#
# 幂等：.env 已存在时全部复用不重写（重跑=升级容器）；已生成的 root 密码只在
# 首次生成时打印，忘记密码走 root 种子规则：改 .env 再 up 不会重置已有账号。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/.env"
DATABASE_URL="${DATABASE_URL:-}"; JWT_SECRET="${BOK_JWT_SECRET:-}"
CP_TOKEN="${BOK_CP_TOKEN:-}"; ROOT_USER="${BOK_ROOT_USERNAME:-admin}"
ROOT_PASS="${BOK_ROOT_PASSWORD:-}"; PORT="${BOK_CP_PORT:-8000}"
IMAGE="${BOK_CP_IMAGE:-ghcr.io/halojerry/bok-voice:latest}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-url) DATABASE_URL="${2:-}"; shift 2;;
    --jwt-secret) JWT_SECRET="${2:-}"; shift 2;;
    --cp-token) CP_TOKEN="${2:-}"; shift 2;;
    --root-user) ROOT_USER="${2:-}"; shift 2;;
    --root-pass) ROOT_PASS="${2:-}"; shift 2;;
    --port) PORT="${2:-}"; shift 2;;
    --image) IMAGE="${2:-}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) sed -n '2,22p' "$0"; exit 0;;
    *) echo "unknown arg: $1（--help 看用法）" >&2; exit 2;;
  esac
done

say() { printf '\033[1;36m[cloud-install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[cloud-install] 失败:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 1) 前置检查 ----
command -v docker >/dev/null || die "未找到 docker——宝塔软件商店装 Docker 套件（或装 Docker CE）"
docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用（宝塔 Docker 套件自带；或 apt install docker-compose-plugin）"
command -v curl >/dev/null || die "未找到 curl"
say "前置检查通过：docker $(docker --version | cut -d, -f1)"

# ---- 2) .env 生成（既有即复用，绝不重写）----
GENERATED_ROOT_PASS=""
if [[ -f "$ENV_FILE" ]]; then
  say "检测到既有 ${ENV_FILE}——复用（升级路径：改镜像 tag 后重跑本脚本即可）。"
  # 既有 .env 的端口/镜像以文件为准（命令行未显式给新值时）。
  FILE_PORT="$(grep -E '^BOK_CP_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2 || true)"
  [[ -n "$FILE_PORT" ]] && PORT="$FILE_PORT"
  FILE_IMAGE="$(grep -E '^BOK_CP_IMAGE=' "$ENV_FILE" | head -1 | cut -d= -f2 || true)"
  [[ -n "$FILE_IMAGE" ]] && IMAGE="$FILE_IMAGE"
else
  [[ -n "$DATABASE_URL" ]] || { [[ -t 0 ]] && read -r -p "Supabase DATABASE_URL（session pooler 5432 端，见 .env.example 格式说明）: " DATABASE_URL; }
  [[ -n "$DATABASE_URL" ]] || die "缺 --database-url（或 env DATABASE_URL）；格式见 .env.example——必须 pooler 域名:5432，禁 6543/直连 db.* 域名"
  [[ "$DATABASE_URL" == *pooler.supabase.com* ]] || say "提醒：连接串不是 pooler 域名——直连 db.*.supabase.com 是 IPv6-only，多数容器出网连不上"
  [[ -n "$JWT_SECRET" ]] || JWT_SECRET="$(openssl rand -hex 32)"
  [[ -n "$CP_TOKEN" ]] || CP_TOKEN="$(openssl rand -hex 24)"
  if [[ -z "$ROOT_PASS" ]]; then
    GENERATED_ROOT_PASS="$(openssl rand -hex 8)"
    ROOT_PASS="$GENERATED_ROOT_PASS"
  fi
  cat > "$ENV_FILE" <<EOF
# 由 install.sh 生成于 $(date '+%F %T')——手改后重跑本脚本不会覆盖本文件。
BOK_AUTH_REQUIRED=1
DATABASE_URL=$DATABASE_URL
BOK_JWT_SECRET=$JWT_SECRET
BOK_CP_TOKEN=$CP_TOKEN
BOK_ROOT_USERNAME=$ROOT_USER
BOK_ROOT_PASSWORD=$ROOT_PASS
BOK_CP_PORT=$PORT
BOK_CP_IMAGE=$IMAGE
EOF
  chmod 600 "$ENV_FILE"
  say ".env 已生成（0600）。警告：BOK_JWT_SECRET 泄露=全部 token 可伪造；BOK_CP_TOKEN 是机器通道钥匙，agent worker 环境必须带同值。"
  [[ -n "$GENERATED_ROOT_PASS" ]] && say "root 密码（自动生成，仅此一次打印，请立即保存）：$ROOT_USER / $GENERATED_ROOT_PASS"
fi

export BOK_CP_PORT="$PORT" BOK_CP_IMAGE="$IMAGE"

# ---- 3) 配置校验（:? 守卫在这里暴露缺项）----
docker compose -f "$HERE/docker-compose.yml" --env-file "$ENV_FILE" config >/dev/null \
  || die "compose 配置校验失败——检查 $ENV_FILE 必填项"
say "compose 配置校验通过（端口 $PORT / 镜像 ${IMAGE}）"
[[ $DRY_RUN -eq 1 ]] && { say "--dry-run：未起容器，配置已就绪。"; exit 0; }

# ---- 4) 起容器并轮询健康（docker-proxy 先于 uvicorn 监听是已知时差，必须显式轮询）----
say "拉镜像+起容器（国内拉 GHCR 慢见 README §1 镜像加速/搬运两条路）……"
docker compose -f "$HERE/docker-compose.yml" --env-file "$ENV_FILE" up -d
HEALTH=""
for i in $(seq 1 60); do
  HEALTH="$(curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)"
  [[ "$HEALTH" == *'"ok":true'* ]] && { say "CP 就绪（第 ${i} 次轮询，首次启动含 Supabase 幂等迁移建表）"; break; }
  sleep 2
done
[[ "$HEALTH" == *'"ok":true'* ]] || { docker compose -f "$HERE/docker-compose.yml" logs --tail 50 cp || true; die "/health 未就绪——上方为容器日志（多为 DATABASE_URL 不通：检查 pooler 串/出网）"; }

# ---- 5) 三项验收：静态站豁免（非 401）+ root 登录（真 JWT）----
CODE="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
[[ "$CODE" != "401" ]] || die "裸 GET / 返回 401（登录页被拦）——版本过旧，请用 v0.2.0+ 镜像"
say "静态站 GET / -> ${CODE}（404=本镜像无静态产物属正常，401=异常已拦）"
RU="$(grep -E '^BOK_ROOT_USERNAME=' "$ENV_FILE" | cut -d= -f2)"
RP="$(grep -E '^BOK_ROOT_PASSWORD=' "$ENV_FILE" | cut -d= -f2)"
LOGIN="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$RU\",\"password\":\"$RP\"}" 2>/dev/null || true)"
if [[ "$LOGIN" == *'"token"'* ]]; then
  say "root 登录验收通过（真 JWT）——云端部署完成。"
else
  say "提醒：root 登录未通过（账号可能此前已存在、密码是旧值）——非致命，可用面板路径自行验证。"
fi

# ---- 6) 打印节点装机一条龙（交给客户机房照抄；全程零 GitHub——脚本与包都从本 CP 拉）----
CP_TOKEN_OUT="$(grep -E '^BOK_CP_TOKEN=' "$ENV_FILE" | cut -d= -f2)"
cat <<EOF

══════════════════════════════════════════════════════════════
 云端就绪。下一步（每客户）：
   0) 发版工件就位（装机前一次性）：打包机跑
        scripts/build_node_pkg.sh <版本> && scripts/build_runtime_pkg.sh <版本>
      云主机 deploy/cloud/ 内跑
        ./publish_node_pkg.sh <版本>
   1) root 登录管理台建客户 admin 账号 + 签发节点 license：
      curl -X POST http://HOST:${PORT}/api/nodes/licenses \\
        -H "Authorization: Bearer <root JWT 或 ${CP_TOKEN_OUT:0:6}…机器token>" \\
        -H 'Content-Type: application/json' -d '{"max_nodes":<盒数>,"note":"<客户>"}'
   2) 把下面命令交客户机房（Linux/bash 节点；填 license key 与节点内网 IP）：
      curl -fsSL -H "Authorization: Bearer bokn_xxx" \\
        https://<云域名>/api/nodes/downloads/bootstrap/latest/bootstrap-node.sh | bash -s -- \\
        --cp-url https://<云域名> \\
        --license-key bokn_xxx \\
        --livekit-url ws://<节点内网IP>:7880
      Windows 节点（管理员 PowerShell，自举+装常驻服务）：
        curl -fsSL -H "Authorization: Bearer bokn_xxx" \\
          https://<云域名>/api/nodes/downloads/bootstrap/latest/install-node.ps1 -o install-node.ps1
        .\\install-node.ps1 -CpUrl https://<云域名> -LicenseKey bokn_xxx \\
          -LivekitUrl ws://<节点内网IP>:7880 -Fetch -InstallService
══════════════════════════════════════════════════════════════
EOF
