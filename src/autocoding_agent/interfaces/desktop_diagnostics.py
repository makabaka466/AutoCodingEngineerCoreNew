"""Tk 主循环卡顿定位。监视线程只读主线程帧，不访问 Tk 控件或业务数据。"""

import logging
import sys
import threading
import time
from functools import wraps
from pathlib import Path

logger = logging.getLogger("autocoding_agent.desktop")


def timed_ui(operation):
    @wraps(operation)
    def measured(*args, **kwargs):
        started = time.monotonic()
        try:
            return operation(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 0.5:
                logger.warning("desktop_slow_operation operation=%s elapsed_ms=%d",
                               operation.__name__, int(elapsed * 1000))
    return measured


def monitor_mainloop(root, *, threshold=5.0):
    """同一次停顿仅采样一次，恢复后可再次记录；返回关闭监视器的函数。"""
    thread_id = threading.get_ident()
    stopped = threading.Event()
    last_tick = time.monotonic()

    def heartbeat():
        nonlocal last_tick
        last_tick = time.monotonic()
        if not stopped.is_set():
            root.after(500, heartbeat)

    def monitor():
        sampled = None
        while not stopped.wait(1):
            tick = last_tick
            elapsed = time.monotonic() - tick
            if tick == sampled or elapsed < threshold:
                continue
            sampled = tick
            frame = sys._current_frames().get(thread_id)
            stack = []
            while frame and len(stack) < 25:
                stack.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}"
                             f" {frame.f_code.co_name}")
                frame = frame.f_back
            logger.warning("desktop_mainloop_stall elapsed_ms=%d stack=%s",
                           int(elapsed * 1000), " <- ".join(stack))

    heartbeat()
    threading.Thread(target=monitor, name="ace-ui-watchdog", daemon=True).start()
    return stopped.set
