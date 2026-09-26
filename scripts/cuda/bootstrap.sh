#!/usr/bin/env bash
# Bok CUDA 一键部署包：预检 + venv + sglang 安装 + 模型下载 + 单元渲染。
# 配套文档：docs/CUDA-DEPLOY.md（问题清单 / 一键流程 / ⚠未实测清单）。
#
# 用法:
#   ./bootstrap.sh [--home DIR] [--models-only] [--skip-models] [--yes]
#
# 步骤（任一步失败即停，人话报错）:
#   [1/5] 预检：Linux / nvidia-smi / driver / CUDA / 磁盘 / 内存 / python3.12 / 端口
#   [2/5] venv + pip 镜像 + sglang（钉版本）+ huggingface_hub
#   [3/5] 渲染 systemd 单元与 env 模板到安装目录（**永不写 /etc**，装载归操作员 root）
#   [4/5] 模型下载（HF_ENDPOINT 镜像；repo 为 TODO 时跳过并告警）
#   [5/5] 打印装载指引（cp 单元 → daemon-reload → enable --now → smoke.sh）
#
# 网络铁律：HF 一律走 HF_ENDPOINT 镜像（缺省 hf-mirror.com，国内机直连 HF 会卡死）；
# pip 走 PIP_INDEX_URL（缺省清华镜像），两者都可在 env 覆盖。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- 参数 ----------
BOK_CUDA_HOME="${BOK_CUDA_HOME:-$HOME/bok-cuda}"
MODELS_ONLY=0; SKIP_MODELS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) BOK_CUDA_HOME="${2:-}"; shift 2;;
    --models-only) MODELS_ONLY=1; shift;;
    --skip-models) SKIP_MODELS=1; shift;;
    --yes) shift;;  # 预留：当前无交互问句，保留口径与 install-node.sh 对齐
    -h|--help) sed -n '2,14p' "$0"; exit 0;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done
BOK_CUDA_HOME="${BOK_CUDA_HOME%/}"
MODELS_DIR="$BOK_CUDA_HOME/models"
VENV_PY="$BOK_CUDA_HOME/venv/bin/python"

# ---------- 版本钉子（改前先读注释） ----------
# sglang 当前稳定版（2026-09-18 发布）。钉版理由：
#   * 本包全部启动旗标（--enable-priority-scheduling / --schedule-policy lpm /
#     --retraction-policy priority / radix 默认开）逐一核对过 v0.5.19 源码与
#     0.5.20 文档（docs.sglang.io advanced_features/server_arguments），
#     升大版本前必须重核（旗标随版本漂移是本包点名的坑，见文档 §2）。
#   * 0.5.20 起 PyPI 车道只发 CUDA 13（0.5.19 是最后一个带 CUDA 12 车道的版本，
#     且其 cu129 轮子索引已退役）——所以 driver 门槛是 580 不是 550。
#   * 任务草案里的 `sglang[sampler]` extra 不存在（核对 v0.5.19/v0.5.20
#     pyproject.toml：extras 只有 all/diffusion/ray/tracing/test/dev 等），
#     裸装 sglang 即服务运行时，勿照抄草案。
SGLANG_VERSION="${SGLANG_VERSION:-0.5.20}"
# 与 SGLANG_VERSION 配套的硬门槛（CUDA 13 大版本族 ⇒ driver ≥ 580）：
REQ_DRIVER_MAJOR=580
REQ_CUDA_MAJOR=13
DISK_MIN_GB="${DISK_MIN_GB:-200}"
MEM_MIN_GB="${MEM_MIN_GB:-64}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

fail() { echo "[bootstrap] FAIL: $*" >&2; exit 1; }
warn() { echo "[bootstrap] WARN: $*"; }
step() { echo; echo "=== [$1] $2 ==="; }

# ================= [1/5] 预检 =================
step "1/5" "预检（Linux / GPU / driver / CUDA / 磁盘 / 内存 / python / 端口）"

[[ "$(uname -s)" == "Linux" ]] || fail "本脚本只支持 Linux 服务器（当前 $(uname -s)）。Mac 栈请用 tools/bok.py serve，勿混装。"

command -v nvidia-smi >/dev/null 2>&1 || fail "找不到 nvidia-smi——NVIDIA driver 未装或不在 PATH。先装 driver（≥${REQ_DRIVER_MAJOR}），再跑本脚本。"

SMI_OUT="$(nvidia-smi)" || fail "nvidia-smi 执行失败（GPU 不在场 / driver 与内核模块不匹配）。"
# 解析：driver 走 --query-gpu（表头 awk $NF 会取到边框竖线，别用）；CUDA 上限
# 只在表头一行（CUDA Version = 该 driver 支持的最高 toolkit 版本）。
DRIVER_VER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
CUDA_CAP="$(sed -n 's/.*CUDA Version: \([0-9.]*\).*/\1/p' <<<"$SMI_OUT" | head -1)"
[[ -n "$DRIVER_VER" && -n "$CUDA_CAP" ]] || fail "无法从 nvidia-smi 解析 driver/CUDA 版本。输出头几行：$(head -3 <<<"$SMI_OUT")"
DRIVER_MAJOR="${DRIVER_VER%%.*}"
CUDA_MAJOR="${CUDA_CAP%%.*}"
echo "  GPU        : $(awk -F': ' '/Product Name/ {print $2; exit}' <<<"$SMI_OUT")"
echo "  driver     : ${DRIVER_VER}（要求 ≥ ${REQ_DRIVER_MAJOR}）"
echo "  CUDA 上限  : ${CUDA_CAP}（要求 ≥ ${REQ_CUDA_MAJOR}.0）"
VRAM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)"
echo "  显存       : $((VRAM_MIB / 1024)) GiB（9B×2 建议 ≥45 GiB，见 docs/CUDA-DEPLOY.md 预算表）"
(( DRIVER_MAJOR >= REQ_DRIVER_MAJOR )) || fail "driver ${DRIVER_VER} 低于 ${REQ_DRIVER_MAJOR}：sglang ${SGLANG_VERSION} 的轮子是 CUDA 13 车道，CUDA 13 大版本族最低 driver 就是 580（550 只够 CUDA 12.x）。选择：①升级 driver 到 ≥580（推荐）；②降级 SGLANG_VERSION=0.5.19——但 0.5.19 的 cu129 轮子索引已退役，实际仍要 driver 方案，勿抱幻想。"
(( CUDA_MAJOR >= REQ_CUDA_MAJOR )) || fail "nvidia-smi 报告 CUDA 上限 ${CUDA_CAP}，低于 ${REQ_CUDA_MAJOR}.0——同一根因（driver 太旧）。升级 driver 后重跑。"

DISK_AVAIL_GB="$(df -BG --output=avail "$BOK_CUDA_HOME" 2>/dev/null | tail -1 | tr -dc '0-9' || true)"
if [[ -z "$DISK_AVAIL_GB" ]]; then
  mkdir -p "$BOK_CUDA_HOME"
  DISK_AVAIL_GB="$(df -BG --output=avail "$BOK_CUDA_HOME" | tail -1 | tr -dc '0-9')"
fi
echo "  磁盘可用   : ${DISK_AVAIL_GB}G（要求 ≥ ${DISK_MIN_GB}G，含 TTS SFT 阶段 C 余量）"
(( DISK_AVAIL_GB >= DISK_MIN_GB )) || fail "磁盘可用 ${DISK_AVAIL_GB}G < ${DISK_MIN_GB}G。9B×2 模型 ~40G + sglang 轮子 ~10G + 缓存/日志/SFT 余量，200G 是底线。"

MEM_AVAIL_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
echo "  内存       : ${MEM_AVAIL_MB}M（要求 ≥ ${MEM_MIN_GB}G）"
(( MEM_AVAIL_MB >= MEM_MIN_GB * 1024 )) || fail "内存 ${MEM_AVAIL_MB}M < ${MEM_MIN_GB}G。权重加载/预处理阶段 host 内存是硬需求。"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "找不到 ${PYTHON_BIN}。Ubuntu 24.04 自带；22.04 用 deadsnakes PPA：add-apt-repository ppa:deadsnakes/ppa && apt install python3.12 python3.12-venv。或用 PYTHON_BIN= 指定已有解释器（需 ≥3.10 且 <3.14）。"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<(3,14) else 1)' \
  || fail "$PYTHON_BIN 版本不在 [3.10, 3.14)——sglang 与本仓依赖都要求 3.10+。"

if command -v ss >/dev/null 2>&1; then
  for p in 18100 18101; do
    ss -ltn "( sport = :$p )" 2>/dev/null | grep -q ":$p" && fail "端口 $p 已被占用（本包 sglang A/judge 车道专用端口，与本地区 1235/1237 有意错开）。先腾端口或在 env.example 调 SGLANG_PORT_*。"
  done
fi
echo "  预检通过"

# ================= [2/5] venv + pip =================
if (( MODELS_ONLY == 0 )); then
step "2/5" "venv + pip 镜像 + sglang ${SGLANG_VERSION}（首次安装含 torch，可能 10-30 分钟）"

[[ -d "$BOK_CUDA_HOME/venv" ]] && warn "venv 已存在：$BOK_CUDA_HOME/venv（复用；要重建先删）。"

if [[ ! -d "$BOK_CUDA_HOME/venv" ]]; then
  mkdir -p "$BOK_CUDA_HOME"
  "$PYTHON_BIN" -m venv "$BOK_CUDA_HOME/venv" || fail "venv 创建失败（缺 python3.12-venv 包？apt install python3.12-venv）。"
fi

PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
echo "  pip index  : ${PIP_INDEX_URL}（PIP_INDEX_URL 可覆盖回官方）"
"$VENV_PY" -m pip install --upgrade pip --index-url "$PIP_INDEX_URL" >/dev/null \
  || fail "pip 升级失败（检查网络/镜像可达性）。"

# 注意：不装任何 extra——`sglang[sampler]` 不存在（见文件头注释），extras 目录里
# 的 all/diffusion/ray/tracing 都是本包用不上的可选面。
"$VENV_PY" -m pip install "sglang==${SGLANG_VERSION}" "huggingface_hub>=0.34" \
  --index-url "$PIP_INDEX_URL" \
  || fail "sglang==${SGLANG_VERSION} 安装失败。排查顺序：①镜像同步延迟（换官方 index 重试）②driver/CUDA 门槛（回到 [1/5]）③磁盘。"
"$VENV_PY" -c "import sglang; print('  sglang', sglang.__version__, 'OK')" || fail "sglang 导入失败——安装不完整，勿继续。"
echo "  venv 就绪  : $VENV_PY"
fi

# ================= [3/5] 渲染单元与 env 模板 =================
step "3/5" "渲染 systemd 单元 + env 模板 → ${BOK_CUDA_HOME}（本脚本永不写 /etc）"

UNIT_OUT="$BOK_CUDA_HOME/units"
mkdir -p "$UNIT_OUT" "$BOK_CUDA_HOME/bin"
for f in sglang-a.service sglang-judge.service bok-node.service; do
  sed -e "s|__BOK_CUDA_HOME__|$BOK_CUDA_HOME|g" \
      "$SCRIPT_DIR/units/$f" > "$UNIT_OUT/$f"
done
# env 模板：把模型目录占位填成实际路径，操作员拷到 /etc/bok-cuda/bok-cuda.env 再填值。
sed -e "s|__BOK_CUDA_HOME__|$BOK_CUDA_HOME|g" \
    -e "s|__MODELS_DIR__|$MODELS_DIR|g" \
    "$SCRIPT_DIR/env.example" > "$BOK_CUDA_HOME/env.rendered.example"
# node_agent 包装脚本（凭据只从 env 文件进 argv，零密钥字面量）。
sed -e "s|__BOK_CUDA_HOME__|$BOK_CUDA_HOME|g" \
    "$SCRIPT_DIR/bok-node-start.sh" > "$BOK_CUDA_HOME/bin/bok-node-start.sh"
chmod +x "$BOK_CUDA_HOME/bin/bok-node-start.sh"
echo "  单元       : $UNIT_OUT/{sglang-a,sglang-judge,bok-node}.service"
echo "  env 模板   : $BOK_CUDA_HOME/env.rendered.example → 拷到 /etc/bok-cuda/bok-cuda.env（600）"
echo "  node 包装  : $BOK_CUDA_HOME/bin/bok-node-start.sh"

# ================= [4/5] 模型下载 =================
if (( SKIP_MODELS == 1 )); then
  step "4/5" "模型下载（--skip-models，跳过）"
elif [[ ! -f "$SCRIPT_DIR/models.env" ]]; then
  step "4/5" "模型下载（缺 models.env，跳过并告警）"
  warn "找不到 $SCRIPT_DIR/models.env——请 cp models.env.example models.env 并按选型填 MODEL_*__REPO（TODO 占位勿臆造仓库名），再重跑 --models-only。"
else
  step "4/5" "模型下载（HF_ENDPOINT 镜像）"
  [[ -x "$VENV_PY" ]] || fail "venv 不存在（${VENV_PY}）——先跑完整 bootstrap（去掉 --models-only）装好 huggingface_hub 再下模型。"
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/models.env"
  HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
  export HF_ENDPOINT
  echo "  HF mirror  : ${HF_ENDPOINT}（HF_ENDPOINT 可覆盖）"
  VENV_PY="$BOK_CUDA_HOME/venv/bin/python"
  download_model() { # $1=repo  $2=dirname
    local repo="$1" dir="$2"
    if [[ -z "$repo" || "$repo" == TODO* ]]; then
      warn "MODEL_${dir^^}_REPO 未定稿（TODO 占位）——跳过 $dir；对应 sglang 单元在填定稿前起不来（smoke.sh 会点名）。"
      return 0
    fi
    echo "  下载 $repo → $MODELS_DIR/$dir ..."
    "$VENV_PY" - "$repo" "$MODELS_DIR/$dir" <<'PYEOF'
import sys
from huggingface_hub import snapshot_download
repo, local_dir = sys.argv[1], sys.argv[2]
snapshot_download(repo_id=repo, local_dir=local_dir, max_workers=4)
print(f"  done: {repo}")
PYEOF
  }
  download_model "${MODEL_A_REPLY_REPO:-}" "${MODEL_A_REPLY_DIRNAME:-a_reply}"
  download_model "${MODEL_JUDGE_REPO:-}"   "${MODEL_JUDGE_DIRNAME:-judge}"
  [[ -z "${MODEL_MT_REPO:-}" || "${MODEL_MT_REPO:-}" == TODO* ]] || \
    download_model "$MODEL_MT_REPO" "${MODEL_MT_DIRNAME:-mt}"
fi

# ================= [5/5] 装载指引 =================
step "5/5" "装载指引（操作员 root 逐字执行；本包永不代写 /etc）"
cat <<GUIDE

  # 1) env 文件（先填值再装载——缺文件单元拒绝启动）
  install -d -m 700 /etc/bok-cuda
  cp $BOK_CUDA_HOME/env.rendered.example /etc/bok-cuda/bok-cuda.env
  $EDITOR /etc/bok-cuda/bok-cuda.env && chmod 600 /etc/bok-cuda/bok-cuda.env

  # 2) 单元装载
  cp $UNIT_OUT/*.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now sglang-a.service sglang-judge.service bok-node.service

  # 3) 验证（开窗清单）
  $SCRIPT_DIR/smoke.sh

  日志: journalctl -u sglang-a -f   （sglang-judge / bok-node 同式）
GUIDE
echo
echo "[bootstrap] OK：安装目录 ${BOK_CUDA_HOME}。后续排坑对照 docs/CUDA-DEPLOY.md。"
