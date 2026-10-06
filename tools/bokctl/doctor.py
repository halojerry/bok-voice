#!/usr/bin/env python
"""doctor 域(doctor --packaged 体检/供应商健康汇总/内存与 GPU 姿态;G2 W② 从 core 搬出,
搬运纪律=穿模块对象调用)。

- 本模块只 `from bokctl import core` 拿模块对象:凡仍住在 core 的名字
  (CORE_PORTS/healthy/_worker_ports/_probe_worker/_http_call/
  _provider_health_summary 等一律 `core.X` 调用时取——patch 与后续域搬运在
  core 侧保持可见(patch 缝=模块属性);
  models 域件(MODELS/model_dir/_lmstudio_models_dir/_llm_draft_enabled/
  _mt_llm_model/_settle_llm_model 等)穿 `models.X` 取(models 波新例);
  路径/平台锚(platform_key/is_packaged/is_mac/is_linux/app_data_dir/
  sidecar_python/bundled_*/_embedded_livekit)paths 波(2026-10-04)后穿
  `paths.X` 取。
- 本域自有函数(_nvidia_gate/_doctor_gpu_gate/_import_ok/_doctor_minimax_tts/
  _model_present/_doctor_draft_warning/_swap_used_gb/_warn_memory_posture/
  _doctor_queue_proxy_lease_timeouts/_probe_llm/_provider_health_fails/cmd_doctor)
  域内裸名互调(同模块全局=call-time 可 patch)。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把 doctor 域
  名字改道 bokctl.doctor);facade 读用 bok.doctor.X。
"""
from __future__ import annotations

import ipaddress
import json
import os
import platform as _platform
import re
import subprocess
import time
from pathlib import Path

from bokctl import core, models, paths


def _provider_health_fails(summary: dict | None) -> list[str]:
    """doctor 的 fail/warning 消息（纯函数）：近窗配额死/限流命中才出消息。"""
    if not summary or not summary.get("degraded"):
        return []
    q = summary.get("quota_2056") or {}
    rl = summary.get("rate_limit") or {}
    bits = []
    if q.get("count"):
        bits.append(f"配额死(2056)x{q['count']} last={q.get('last_hit')}")
    if rl.get("count"):
        bits.append(f"限流x{rl['count']} statuses={rl.get('statuses')} last={rl.get('last_hit')}")
    undated = summary.get("undated") or 0
    suffix = f"（另有 {undated} 条无时基打点未计入）" if undated else ""
    return ["MiniMax 云 TTS 近窗异常: " + "; ".join(bits) + suffix + " —— 云端合成会静默劣化到垫话/watchdog 兜底"]


def _probe_llm(base_url: str = "http://127.0.0.1:1235/v1",
               timeout_s: float | None = None,
               model: str = "",
               prompt: str = "hi") -> tuple[bool, str]:
    """LLM 功能探针:端口 UP ≠ 能用——mlx_lm 被 wedge(解码排队/缓存坍缩)时
    /v1/models 照开 200,通话顿成狗而健康面全绿。发 max_tokens=1 真 prefill
    测往返;超时预算 BOK_DOCTOR_LLM_PROBE_TIMEOUT_S(默认 10s;空闲 >2h 后
    权重页入 ~40s 会一次假警,重跑一次区分:冷启动第二次会快)。
    model 缺省取 /v1/models 的绝对路径 id——repo id 会触发 HF hub 解析;
    显式传 model(:1236 MT 探针)则忽略扫描结果直接用,同样必须是本地路径。
    显式档仍先 GET /v1/models 属有意保留(评审 P2-1):同一 mlx_lm 进程同时
    服务两端点,/models 探不动即进程不可用,短路 FAIL 语义正确,省一次
    information GET 的重构不值当。
    prompt 对 MT 档必须传代表性长句(如「Translate to English: 你好世界」)——
    Hy-MT2 的 chat template 对超短 ASCII 输入会 list index out of range 404
    (2026-09-19 实测,生产链路恒走 _mt_prompt 长模板不受影响)。"""
    if timeout_s is None or timeout_s <= 0:
        try:
            timeout_s = float(os.environ.get("BOK_DOCTOR_LLM_PROBE_TIMEOUT_S", "10") or 10)
        except ValueError:
            timeout_s = 10.0
    model = str(model or "").strip()
    try:
        status, raw = core._http_call(f"{base_url.rstrip('/')}/models", timeout_s=timeout_s)
        if status != 200:
            return False, f"FAIL (HTTP {status} on /v1/models)"
        ids = [str(m.get("id") or "")
               for m in json.loads(raw.decode()).get("data", [])]
        if not model:
            model = next((i for i in ids if i.startswith("/")), ids[0] if ids else "")
            if not model:
                return False, "FAIL (/v1/models 空列表)"
        body = json.dumps({"model": model, "stream": False, "temperature": 0,
                           "max_tokens": 1,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        t0 = time.monotonic()
        status, raw = core._http_call(
            f"{base_url.rstrip('/')}/chat/completions", "POST",
            body=body, headers={"Content-Type": "application/json"}, timeout_s=timeout_s,
        )
        if status != 200:
            return False, f"FAIL (HTTP {status} on chat/completions)"
        json.loads(raw.decode())
        ms = (time.monotonic() - t0) * 1000
        verdict = "ok" if ms <= 3000 else "SLOW(>3s 预算,查排队/缓存)"
        return True, f"{verdict} {ms:.0f}ms (model={Path(model).name})"
    except Exception as exc:  # noqa: BLE001 - 探针只报告,不抛
        return False, (f"FAIL ({exc}; >{timeout_s:.0f}s 疑似 wedge 或冷启动页入,重跑一次区分)")


def _nvidia_gate() -> tuple[bool, str]:
    """Windows LLM requires an NVIDIA GPU: driver >= 550, VRAM >= 8GB."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return False, "NVIDIA GPU 未检测到（nvidia-smi 无输出）"
        line = out.stdout.strip().splitlines()[0]
        driver, mem = [p.strip() for p in line.split(",")]
        driver_major = int(driver.split(".")[0])
        mem_mb = int(float(mem))
        if driver_major < 550:
            return False, f"NVIDIA 驱动 {driver} 过低，需要 >= 550"
        if mem_mb < 8192:
            return False, f"显存 {mem_mb}MB < 8192MB（建议 >= 8GB）"
        return True, f"NVIDIA OK driver={driver} vram={mem_mb}MB"
    except Exception as exc:
        return False, f"nvidia-smi 不可用: {exc}"


def _doctor_gpu_gate(packaged: bool, fails: list[str]) -> None:
    """Windows/Linux(CUDA) NVIDIA 硬件门禁（独立于虚拟声卡检测）。

    曾误缩进在 `if not va_ok:` 下——装了 VB-CABLE 的 Windows 机器直接跳过 GPU
    检查（打包 doctor 漏报），而没装虚拟声卡的 mac 反而被拖去跑 nvidia-smi
    （packaged 模式误报 fail）。d0035c3 原始意图就是挂在 Windows 分支
    （nvidia-smi 是 Windows LLM=CUDA llama.cpp 的前置，mac 无此检查）。
    Linux 扩档（2026-09-22，runbook §5⑥）：GPU 节点同为 CUDA llama.cpp 前置，
    同门同判（nvidia-smi/驱动 ≥550/显存 ≥8GB 同阈值）；mac 仍无此检查。
    """
    if os.name != "nt" and not paths.is_linux():
        return
    ok, msg = _nvidia_gate()
    print(f"nvidia gate: {msg}")
    if packaged and not ok:
        fails.append(msg)


def _import_ok(py: Path, module: str) -> bool:
    try:
        r = subprocess.run(
            [str(py), "-c", f"import {module}"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return False
    return r.returncode == 0


def _doctor_minimax_tts(data: Path, fails: list[str]) -> None:
    """MiniMax 音色校验探针：settings tts.provider=minimax 时，要求 api_key 非空
    且至少一个已配音色能在 MiniMax get_voice 下解析。

    - key 缺失 → 静默跳过（凭据永不入码，缺失本身唔算故障——用户可能纯用本地 TTS）。
    - 无任何已配音色 → 记 warning（设置页漏配，通话会 beep）。
    - get_voice 可达且列出了音色、但已配音色全部唔喺列表 → 硬 fail（确定性错配）。
    - 网络不可达/接口异常 → 只提示，唔 fail（doctor 唔依赖外网）。
    """
    db = data / "bok_voice.db"
    if not db.exists():
        print("minimax tts voice: skipped (no settings db)")
        return
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT tts_json FROM global_settings WHERE id='global'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        print(f"minimax tts voice: skipped (settings db unreadable: {exc})")
        return
    if not row:
        print("minimax tts voice: skipped (settings empty)")
        return
    try:
        tts = json.loads(row[0] or "{}")
    except Exception:
        tts = {}
    provider = str(tts.get("provider") or "").strip().lower()
    if provider not in ("minimax", "minimax_streaming"):
        return  # 非 MiniMax 供应商:静默返回
    api_key = str(tts.get("api_key") or "").strip() or os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        # 静默跳过:未配 key 唔係结构故障。
        print("minimax tts voice: skipped (provider=minimax but api_key empty)")
        return
    voices = [str(tts.get(k) or "").strip() for k in ("speaker", "speaker_zh", "speaker_cantonese", "speaker_en")]
    configured = [v for v in voices if v]
    if not configured:
        # 按本函数 docstring 的原意记 warning 而非硬 fail:e20ed7a 时代的「全空=静音」
        # 前提已失效——A 线运行时有默认音色兜底(Cantonese_crisp_news_anchor_vv2,
        # 三语通用、空音色防 beep),漏配只是设置页待补,不是发布阻断项。
        msg = (
            "minimax tts: provider=minimax 且已配 key,"
            "但 speaker_zh/cantonese/en 全空(运行时走默认音色兜底,设置页可补配)"
        )
        print(f"minimax tts voice: warning ({msg})")
        return
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = "https://api.minimax.chat/v1" if region in {"intl", "global", "chat"} else "https://api.minimax.cn/v1"
    root = base.split("/v1/")[0]
    try:
        status, raw = core._http_call(
            f"{root}/v1/get_voice", "POST",
            body=json.dumps({"voice_type": "all"}).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout_s=8,
        )
        if status != 200:
            print(f"minimax tts voice: warning (get_voice HTTP {status})")
            return
        body = json.loads(raw.decode())
    except Exception as exc:
        # 探针够唔到外网(离线 doctor/网络抖动)→ 只提示,唔当 fail。
        print(f"minimax tts voice: warning (probe unreachable: {exc})")
        return
    if str((body.get("base_resp") or {}).get("status_code")) not in ("0", "None"):
        print(f"minimax tts voice: warning (get_voice base_resp={body.get('base_resp')})")
        return
    found: set[str] = set()

    def _collect(node) -> None:
        if isinstance(node, dict):
            vid = node.get("voice_id")
            if isinstance(vid, str):
                found.add(vid)
            for v in node.values():
                _collect(v)
        elif isinstance(node, list):
            for v in node:
                _collect(v)

    _collect(body.get("data") or {})
    if not found:
        print("minimax tts voice: warning (get_voice returned no voices)")
        return
    resolved = [v for v in configured if v in found]
    if resolved:
        print(f"minimax tts voice: ok ({', '.join(resolved)}; {len(found)} voices on account)")
    else:
        msg = f"minimax tts: 已配音色全部唔喺账号音色列表: {configured}"
        print(f"minimax tts voice: FAIL ({msg})")
        fails.append(msg)


def _model_present(repo: str) -> bool:
    """模型在盘判定:app-data 布局优先,mac 再认 lmstudio 布局(~/.lmstudio
    models,与 cmd_download 的 ensure 同款)——9B settle 只以 lmstudio 布局
    在盘时,旧版 doctor 恒报 MISSING 而同一台机 :1237 分明在跑(2026-09-17 修)。"""
    target = models.model_dir(repo)
    if target.exists() and any(target.iterdir()):
        return True
    if paths.is_mac():
        lm = models._lmstudio_models_dir() / repo
        return lm.exists() and any(lm.iterdir())
    return False


def _doctor_draft_warning(current: dict[str, str]) -> str:
    """draft 模型 doctor 警告判定(纯函数,离线可单测):BOK_LLM_DRAFT=1 且
    draft 模型缺席 → 警告文案;其余(默认关/在盘/表无条目)回 ""。

    警告只进 doctor 打印面,**不进 fails**(不判死、不进 packaged 门禁)——
    draft 是 opt-in 特性,缺席时 _llm_draft_flags 回 [] 正常起无 draft 服务,
    功能零损失,不构成「活着但残废」。"""
    if not models._llm_draft_enabled():
        return ""
    repo = current.get("llm_draft", "")
    if not repo or _model_present(repo):
        return ""
    return (f"llm draft: BOK_LLM_DRAFT=1 但模型未在盘 ({repo}) — serve 将无 draft "
            "起 :1235(不 fail);补齐: python tools/bok.py download --only llm_draft")


def _swap_used_gb() -> float:
    """本机 swap 已用 GB（mac=sysctl vm.swapusage / linux=/proc/meminfo；失败=-1）。"""
    try:
        if _platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3
            ).stdout
            m = re.search(r"used\s*=\s*([\d.]+)M", out)
            return float(m.group(1)) / 1024.0 if m else -1.0
        swap = {}
        for ln in Path("/proc/meminfo").read_text().splitlines():
            if ln.startswith(("SwapTotal", "SwapFree")):
                k, v = ln.split(":")
                swap[k] = int(v.strip().split()[0]) / 1048576.0  # kB→GB
        if "SwapTotal" in swap:
            return swap["SwapTotal"] - swap.get("SwapFree", 0.0)
        return -1.0
    except Exception:
        return -1.0


def _warn_memory_posture(fails: list[str], *, packaged: bool) -> None:
    """内存姿态检查（2026-10-01 第十五波,call-dc54f542 根修配套）：swap 挤压会把
    MLX 权重页出→首 token 3-25s（实测 26GB swap 欠账窗口,LLM 轮全灭由罐头垫）。
    >8GB 警告（语音栈常驻 ~12-18GB 统一内存,桌面应用挤占是主要来源）。"""
    used = _swap_used_gb()
    if used < 0:
        print("memory posture: swap 读取失败(跳过)")
        return
    lvl = "ok" if used < 2 else ("warn" if used < 8 else "CRITICAL")
    print(f"memory posture: swap used {used:.1f}GB [{lvl}]")
    if used >= 8:
        msg = (f"swap {used:.0f}GB 挤压——MLX 权重会被页出,首 token 可达 3-25s。"
               "关桌面大户/重启清欠账后再跑语音。")
        print(f"  ⚠ {msg}")
        if packaged:
            fails.append(msg)


# queue_proxy 租约看门狗打点字面量（services/llm-mlx/queue_proxy.py
# `[llm-queue] lease-timeout forced-reclaim ...`）——doctor 计数面单点，防两侧
# 字面量漂移假绿（test_doctor_blind_spots 有源级 pin）。
_LEASE_TIMEOUT_MARKER = "lease-timeout forced-reclaim"


def _doctor_queue_proxy_lease_timeouts(log_dir: Path) -> int | None:
    """llm-proxy.log + settle-proxy.log 的租约看门狗打点合计（None=两份都读不出）。

    队列代理的槽泄漏/断连僵尸回收证据全在这行——doctor 汇总一行数字面
    （informational：历史累计非当前故障，要判窗看行内时间戳）。
    settle-proxy(:1238,2026-10-03 I1) 与 :1235 同码同打点，同扫求和。"""
    total: int | None = None
    for name in ("llm-proxy.log", "settle-proxy.log"):
        try:
            text = (log_dir / name).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        total = (total or 0) + text.count(_LEASE_TIMEOUT_MARKER)
    return total


def cmd_doctor() -> int:
    """Preflight diagnostics. In packaged mode every check is a hard gate."""
    key = paths.platform_key()
    packaged = paths.is_packaged()
    fails: list[str] = []
    print(f"platform: {_platform.system()} ({key})")
    print(f"packaged: {packaged}")
    print(f"app-data: {paths.app_data_dir()}")

    data = paths.app_data_dir()
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor-write"
        probe.write_text("ok")
        probe.unlink()
        print("app-data writable: ok")
    except Exception as exc:
        fails.append(f"app-data 不可写: {exc}")

    _warn_memory_posture(fails, packaged=packaged)

    py = paths.sidecar_python("qwen3-asr-sidecar")
    print(f"runtime python: {py} {'ok' if py.exists() else 'MISSING'}")
    if not py.exists():
        fails.append(f"runtime python missing: {py}")
    else:
        if paths.is_mac():
            for mod, p in (("mlx_audio", py), ("mlx_lm", paths.sidecar_python("llm-mlx"))):
                ok = _import_ok(p, mod)
                print(f"  import {mod}: {'ok' if ok else 'FAIL'}")
                if packaged and not ok:
                    fails.append(f"import {mod} failed")
        else:
            for mod in ("qwen_asr", "torch"):
                ok = _import_ok(py, mod)
                print(f"  import {mod}: {'ok' if ok else 'FAIL'}")
                if packaged and not ok:
                    fails.append(f"import {mod} failed")

    # 本地语义端点判定(LiveKit turn-detector v1-mini):缺失时端点判定退回纯 VAD,
    # 客户思考停顿更容易被提前截断。粤(cantonese)语无官方校准,阈值回退英文档。
    # import 名是 livekit.local_inference(命名空间包)——pip 名 livekit-local-inference
    # 的下划线拼法不是模块路径,v0.2.0 首次 tag 流水线即被此假 MISSING 判死(mac
    # verify 挂 31 分钟后 FAIL,2026-09-15 实证)。
    eot_ok = _import_ok(py, "livekit.local_inference") if py.exists() else False
    print(f"local EOT inference (turn-detector v1-mini): {'ok' if eot_ok else 'MISSING(退回纯 VAD 端点)'}")
    if packaged and not eot_ok:
        fails.append("livekit-local-inference missing (EOT 退回纯 VAD)")

    livekit = paths._embedded_livekit()
    print(f"livekit-server: {livekit if livekit else 'MISSING'}")
    if packaged and livekit is None:
        fails.append("livekit-server missing")

    node = paths.bundled_node()
    print(f"node: {node if node else 'MISSING'}")
    if packaged and node is None:
        fails.append("node missing")

    if not paths.is_mac():
        llama = paths.bundled_llama()
        print(f"llama-server: {llama if llama else 'MISSING'}")
        if packaged and llama is None:
            fails.append("llama-server missing (Windows 需要 CUDA 版)")

    # 虚拟声卡（B 线同传路由用；A 线通话不需要——报告性不判死：纯 A 线部署/
    # CI runner 都没有音频设备）。macOS=BlackHole、Windows=VB-CABLE，两平台
    # 生态不同不能共用；装法见 scripts/setup-virtual-audio.sh|ps1。
    va_ok = core._virtual_audio_present()
    print(f"virtual audio ({'BlackHole' if paths.is_mac() else 'VB-CABLE'}): "
          f"{'ok' if va_ok else 'MISSING(B线同传需要;A线可忽略)'}")
    if not va_ok:
        print("  (一键安装: scripts/setup-virtual-audio."
              f"{'sh' if paths.is_mac() else 'ps1'}；装完重启浏览器)")
    # NVIDIA 门禁独立于虚拟声卡有无（曾误缩进在 if not va_ok 下，见 _doctor_gpu_gate）。
    _doctor_gpu_gate(packaged=packaged, fails=fails)

    current = models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"]
    for name, repo in current.items():
        if not repo:
            continue
        present = _model_present(repo)
        print(f"model {name}: {'ok' if present else 'MISSING'} ({repo})")
        if not present:
            # 模型在 CI/首启前允许缺失：由 setup status 门禁管理，不阻塞 bundle 校验。
            print("  (模型权重不随包，首启向导下载；doctor 不将其视为结构失败)")

    # draft 模型警告(opt-in 特性):只在 BOK_LLM_DRAFT=1 且缺席时出一行——
    # 不进 fails(不判死),缺席时 serve 自动回落无 draft,见 _doctor_draft_warning。
    draft_warn = _doctor_draft_warning(current)
    if draft_warn:
        print(f"  {draft_warn}")

    for name, port in core.CORE_PORTS:
        if name == "llm-raw" and not core.healthy(port) and not core._llm_raw_expected():
            # queue proxy 关（或非 mac）=mlx 直跑 :1235，:1239 设计缺席——
            # 标 skipped 不骗 DOWN（同 cmd_status 先例）。
            print(f"  port {port:<5} ({name}): skipped (queue proxy off)")
            continue
        print(f"  port {port:<5} ({name}): {'UP' if core.healthy(port) else 'DOWN'}")
    # worker 探针读端点本体(TCP UP 对错码/未 register 假活不可见);缺席只打印
    # 不判死——打包 doctor 在栈未起时也要能跑。
    for name, port in core._worker_ports():
        ok, detail = core._probe_worker(port)
        print(f"  worker {port:<5} ({name}): {detail}")
    # LLM 功能探针:端口 UP ≠ 能用,见 _probe_llm docstring; informational
    # 不进 fails——冷启动页入(~40s)会假警,不得卡打包门禁。
    if core.healthy(1235):
        llm_ok, llm_detail = _probe_llm()
        print(f"  llm 功能探针: {llm_detail}")
        if not llm_ok:
            print("    (llm 端口 UP 但 prefill 探针失败——通话会顿;重跑 doctor 区分冷启动)")
    # MT 功能探针(:1236,2026-09-19 同传 2/8 FAIL 实案):mlx_lm 被 repo-id 请求
    # wedge 时 TCP/健康面全绿、生成永挂——模型在盘才探,informational 不进 fails
    # (与 :1235 同款冷启动页入假警语义)。显式传在盘路径(忽略 /v1/models 扫描
    # 结果),prompt 用代表性长句(见 _probe_llm docstring 的 Hy-MT2 短输入坑)。
    if core.healthy(1236):
        mt_model = models._mt_llm_model(current)
        if mt_model and Path(mt_model).exists():
            mt_ok, mt_detail = _probe_llm(
                "http://127.0.0.1:1236/v1", model=mt_model,
                prompt="Translate to English: 你好世界")
            print(f"  mt 功能探针: {mt_detail}")
            if not mt_ok:
                print("    (mt 端口 UP 但 prefill 探针失败——B 线同传会挂死/退主 LLM;"
                      "重跑 doctor 区分冷启动)")
        else:
            # 评审 P2-2:serve 侧「:1236 健康即下发 MT_*」会把非法 model 透传给
            # worker(interpret 守卫兜底回退主 LLM)——诊断面对同一错配不能零输出。
            print(f"  mt 功能探针: SKIP (MT 模型缺失/非法: {mt_model[:60] or 'unset'})"
                  " — B 线已回退主 LLM,查 MT_LLM_MODEL/MODELS 表")

    # a_reply 专线功能探针（:1237，2026-10-02 审计补盲）：P2 翻档后 :1237 是
    # 通话回复主脑（9B 专线），此前 doctor 只探 :1235/:1236——:1237 wedge 时
    # 通话回复直接灭而 doctor 全绿。同 _probe_llm max_tokens=1 形状（显式在盘
    # 模型路径），informational 不进 fails（冷启动页入同 :1235 语义）。
    if core.healthy(1237):
        _ar_model = models._settle_llm_model(current)
        if _ar_model and Path(_ar_model).exists():
            ar_ok, ar_detail = _probe_llm("http://127.0.0.1:1237/v1", model=_ar_model)
            print(f"  a_reply 功能探针(:1237): {ar_detail}")
            if not ar_ok:
                print("    (a_reply 专线端口 UP 但 prefill 探针失败——通话回复会灭;"
                      "重跑 doctor 区分冷启动)")
        else:
            print(f"  a_reply 功能探针(:1237): SKIP (9B 模型缺失/非法: "
                  f"{_ar_model[:60] or 'unset'}) — a_reply 回退 :1235")

    # /api/token 必须是真 JWT（三段式）；否则 A 线 UI 永远“接通失败”。
    if core.healthy(8000):
        try:
            body = json.dumps({"account_id": "acc-001", "room_name": "doctor-probe"}).encode()
            headers = {"Content-Type": "application/json"}
            # B1/B2 起 /api/token 在 auth-on 下要求身份；机器通道（BOK_CP_TOKEN）
            # 直通——auth-on 栈必须带（agent worker 同源同款，serve 与 doctor 同 env）。
            cp_token = os.environ.get("BOK_CP_TOKEN", "").strip()
            if cp_token:
                headers["Authorization"] = f"Bearer {cp_token}"
            status, raw = core._http_call(
                "http://127.0.0.1:8000/api/token", "POST",
                body=body, headers=headers, timeout_s=8,
            )
            if status == 401:
                if not cp_token:
                    msg = ("token endpoint HTTP 401（auth-on 栈要求身份——请带 "
                           "BOK_CP_TOKEN=<serve 同值> 重跑 doctor；agent worker 同理）")
                else:
                    msg = "token endpoint HTTP 401（LiveKit 凭据缺失或服务异常）"
                fails.append(msg)
                print(f"token endpoint: FAIL ({msg})")
            elif status != 200:
                msg = f"token endpoint HTTP {status}（LiveKit 凭据缺失或服务异常）"
                fails.append(msg)
                print(f"token endpoint: FAIL ({msg})")
            else:
                payload = json.loads(raw.decode())
                tok = str(payload.get("participantToken") or "")
                if tok.count(".") == 2:
                    print("token endpoint: ok (real JWT)")
                else:
                    msg = "token endpoint 返回的不是三段式 JWT（疑似旧 sha256 兜底）"
                    fails.append(msg)
                    print(f"token endpoint: FAIL ({msg})")
        except Exception as exc:
            msg = f"token endpoint 不可达: {exc}"
            fails.append(msg)
            print(f"token endpoint: FAIL ({msg})")
    else:
        print("token endpoint: skipped (control-plane down)")

    # P3-C 配套检查（2026-09-17 全量 debug；2026-09-21 Item 4 扩判据）：
    # LIVEKIT_API_SECRET 未配置且（auth-on 或 bind 非回环）时，CP webhook 验签对
    # 无签名请求一律 401 拒收——LiveKit 崩溃补位（participant_left 重派）会静默
    # 失效。与 CP 端闸同判据；仅「回环 bind + 双关 auth-off」保留 fail-open（F2
    # 契约）。纯 informational，不进 packaged 门禁 fails（本地 auth-off 未配
    # secret 是常态）。
    _doc_auth_on = os.environ.get("BOK_AUTH_REQUIRED", "").strip() == "1"
    _doc_lk_secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    _doc_bind = core._cp_bind_host().strip().strip("[]").lower()
    try:
        _doc_exposed = not ipaddress.ip_address(_doc_bind).is_loopback
    except ValueError:
        _doc_exposed = _doc_bind not in ("", "localhost")
    if (_doc_auth_on or _doc_exposed) and not _doc_lk_secret:
        print("webhook secret: MISSING (auth-on/非回环 bind 拒收无签名 webhook)")
    else:
        print(f"webhook secret: {'ok' if _doc_lk_secret else 'n/a (回环+auth-off fail-open)'}")

    # MiniMax 云 TTS 探针:provider=minimax 时校验 api_key 非空 + 至少一个已配音色
    # 能喺账号音色列表解析(key 缺失静默跳过,凭据永不入码)。
    _doctor_minimax_tts(data, fails)

    # M-11（2026-09-23 修复波#3，task-13 F-M1）云配额健康：本地端口全绿 ≠ 云配额
    # 活着——2056 风暴期全健康面绿了 11 分钟。近窗(5min)打点聚合,命中进 fails
    # （「活着但残废」正是 doctor 的管辖面;配额恢复后重跑 doctor 即转绿）。
    _ph = core._provider_health_summary()
    if _ph is None:
        print("  cloud-tts 配额面: n/a (provider health scanner unavailable)")
    else:
        _ph_fails = _provider_health_fails(_ph)
        if _ph_fails:
            for _msg in _ph_fails:
                fails.append(_msg)
            print(
                f"  cloud-tts 配额面: FAIL (近 {_ph['window_s']:.0f}s"
                f" 2056×{_ph['quota_2056']['count']} 限流×{_ph['rate_limit']['count']})"
            )
        else:
            print(f"  cloud-tts 配额面: ok (近 {_ph['window_s']:.0f}s 无 2056/限流打点)")

    # queue_proxy 租约看门狗计数（2026-10-02 审计补盲）：槽泄漏/断连僵尸的
    # 代理侧证据（lease-timeout forced-reclaim）此前无人汇总。informational
    # 不进 fails——历史累计非当前故障；日志缺席静默（dev 非代理拓扑常态）。
    _lease_n = _doctor_queue_proxy_lease_timeouts(paths.app_data_dir() / "logs")
    if _lease_n is not None:
        print(f"doctor: queue_proxy lease_timeouts={_lease_n}")

    if packaged and fails:
        print("\nPACKAGED DOCTOR FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    if fails:
        print("\nwarnings (dev mode, non-blocking):")
        for f in fails:
            print(f"  - {f}")
    print("doctor: OK" if not fails else "doctor: warnings")
    return 0
