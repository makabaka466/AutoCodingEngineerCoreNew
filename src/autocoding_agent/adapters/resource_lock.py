"""进程间短资源互斥；只协调本地I/O，不占用模型调用或新增任务执行槽位。"""

import errno
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from autocoding_agent.ports.runtime import RuntimeInterruptedError


@contextmanager
def resource_lock(
    path: Path, *, timeout_seconds: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """等待资源时可停止；进程异常退出由OS释放锁，计时不计入后续数据库查询超时。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        while True:
            if stop_requested and stop_requested():
                raise RuntimeInterruptedError("等待共享资源时已收到停止请求，尚未执行本次操作。")
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("共享资源仍由其他任务使用，请稍后重试。") from None
                time.sleep(0.02)
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            if stop_requested and stop_requested():
                raise RuntimeInterruptedError("已收到停止请求，尚未执行本次操作。")
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
