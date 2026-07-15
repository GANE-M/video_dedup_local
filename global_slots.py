from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


def _slot_root() -> Path:
    override = os.environ.get("VIDEO_DEDUP_GLOBAL_SLOT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "video-dedup-local" / "locks"
    return Path(tempfile.gettempdir()) / "video-dedup-local" / "locks"


def _try_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b", buffering=0)
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, PermissionError):
        handle.close()
        return None


def _unlock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def _global_slot(
    namespace: str,
    limit: int = 5,
    label: str = "全局任务",
    logger: Callable[[str], None] = print,
    poll_seconds: float = 0.5,
) -> Iterator[int | None]:
    """Wait for one machine-wide named slot and release it on every exit path."""
    limit = int(limit)
    if limit <= 0:
        yield None
        return

    root = _slot_root() / namespace
    slot_name = "ASR 槽位" if namespace.startswith("asr-") else "AI 请求槽位"
    announced_wait = False
    last_wait_log = 0.0
    while True:
        for slot_index in range(limit):
            handle = _try_lock(root / f"slot-{slot_index + 1:02d}.lock")
            if handle is None:
                continue
            logger(f"{label} 获得全局 {slot_name} {slot_index + 1}/{limit}")
            try:
                yield slot_index + 1
            finally:
                _unlock(handle)
                logger(f"{label} 释放全局 {slot_name} {slot_index + 1}/{limit}")
            return

        now = time.monotonic()
        if not announced_wait or now - last_wait_log >= 30.0:
            logger(f"{label} 等待全局 {slot_name}（{limit}/{limit} 正在使用）")
            announced_wait = True
            last_wait_log = now
        time.sleep(max(0.05, poll_seconds))


@contextmanager
def global_asr_slot(
    limit: int = 5,
    label: str = "音频 ASR",
    logger: Callable[[str], None] = print,
    poll_seconds: float = 0.5,
) -> Iterator[int | None]:
    """Wait for one machine-wide ASR slot and release it on every exit path."""
    with _global_slot("asr-v1", limit, label, logger, poll_seconds) as slot:
        yield slot


@contextmanager
def global_llm_slot(
    limit: int = 5,
    label: str = "AI 请求",
    logger: Callable[[str], None] = print,
    poll_seconds: float = 0.25,
) -> Iterator[int | None]:
    """Limit all GUI processes to one shared pool of outbound LLM requests."""
    with _global_slot("llm-v1", limit, label, logger, poll_seconds) as slot:
        yield slot
