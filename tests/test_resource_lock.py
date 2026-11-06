"""共享资源锁等待可取消，持锁进程终止后由系统释放，不留下永久租约。"""

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from autocoding_agent.adapters.resource_lock import resource_lock
from autocoding_agent.ports.runtime import RuntimeInterruptedError


def _hold(path, acquired):
    with resource_lock(Path(path)):
        acquired.set()
        # 不在将被强制终止的进程里等待共享Event，避免损坏其条件锁后阻塞测试清理。
        time.sleep(10)


def test_wait_can_stop_and_dead_owner_releases_cross_process_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    path = tmp_path / "target.lock"
    process = context.Process(target=_hold, args=(str(path), acquired))
    process.start()
    try:
        assert acquired.wait(timeout=5)
        cancel = Event()
        waiting = Event()

        def waiting_stop():
            waiting.set()
            return cancel.is_set()

        def attempt():
            with resource_lock(path, stop_requested=waiting_stop):
                raise AssertionError("资源仍由另一进程持有，不能进入操作。")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(attempt)
            assert waiting.wait(timeout=3)
            cancel.set()
            with pytest.raises(RuntimeInterruptedError, match="停止请求"):
                future.result(timeout=3)
        process.terminate()
        process.join(timeout=5)
        with resource_lock(path, timeout_seconds=2):
            assert not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
