"""双派发守卫:同一 room 只允许一个 A 线 agent job 进入(2026-09-28)。

取证(call-0105a539,app-data logs/agent.log):同一房间出现两个 dispatch_id,
两个 job 子进程并跑整通——双开场白相隔 285ms、双份 LLM 回答互相打架、GPU
翻倍拖慢全链 → 2s fallback 道歉风暴(同一句致歉 gen=llm 连出 8 轮)。客户
体感即「多轮对话后非常容易卡死」。派发层(LiveKit 显式 dispatch)为何偶发
双发未明,守卫做在 worker 入口:后到 job 让位。

设计约束:
- flock 互斥:job 由 JobExecutorProc 子进程执行,跨进程互斥;持锁进程死亡
  锁自动释放——worker 崩溃/重启后的重派发不受阻,零 stale-lock 治理。
- fail-open:锁文件打开/读写任何异常一律放行(守卫绝不比双派发更糟);
  BOK_ROOM_CLAIM=0 整闸关(测试/特殊多 worker 场景逃生)。
- Windows 兼容:fcntl 缺席回落 msvcrt.locking;再不行 fail-open。
- 只包 A 线(bok-voice)。B 线 fwd/rev 两 worker 同房是设计,不接。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_KILL_SWITCH_ENV = "BOK_ROOM_CLAIM"
_DIR_ENV = "BOK_ROOM_CLAIM_DIR"
# 锁文件名字符集白名单：房名是 CP 派发的 id（call-xxx 形态），但拼路径前
# 就地收敛——穿越段（../、路径分隔、空字节）一律映射为 _，越界字符不进文件名。
_ROOM_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def claims_dir() -> Path:
    """app-data/run/room-claims(与 bok.py app_data_dir / tts_cache 同布局)。"""
    explicit = os.environ.get(_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit)
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    else:
        base = Path.home() / ".local/share"
    return base / "BokVoice" / "run" / "room-claims"


def room_claim_enabled() -> bool:
    return os.environ.get(_KILL_SWITCH_ENV, "1") != "0"


class RoomClaim:
    """单 room 互斥令牌:acquire() 成功后持有至 release()/进程退出。"""

    def __init__(self, room: str) -> None:
        self.room = _ROOM_SAFE.sub("_", str(room or ""))[:128] or "unknown"
        self._path = claims_dir() / f"{self.room}.lock"
        self._fd: int | None = None

    def acquire(self, job_id: str = "") -> tuple[bool, str]:
        """尝试认领;(拿到?, 持有者描述)。竞争失败返回 (False, holder)。"""
        if not room_claim_enabled():
            return True, "kill-switch-off"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            return True, f"claim-open-failed({exc.__class__.__name__})"
        ok, holder = self._try_lock(fd)
        if not ok:
            try:
                os.close(fd)
            except OSError:
                pass
            return False, holder
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(
                fd,
                f"pid={os.getpid()} job={job_id} ts={int(time.time())}\n".encode(),
            )
        except OSError:
            pass
        self._fd = fd
        return True, ""

    def _try_lock(self, fd: int) -> tuple[bool, str]:
        try:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True, ""
            except OSError:
                return False, self._read_holder(fd)
        except ImportError:
            pass
        try:
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True, ""
            except OSError:
                return False, self._read_holder(fd)
        except ImportError:
            # 两个实现都不在(非 POSIX 非 NT)→ fail-open
            return True, "no-lock-impl"

    @staticmethod
    def _read_holder(fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 256) or b""
            return data.decode("utf-8", "replace").strip() or "unknown"
        except OSError:
            return "unknown"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except ImportError:
            try:
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
