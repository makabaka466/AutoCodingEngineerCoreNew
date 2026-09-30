"""独立服务启动入口；HTTP 和 Worker 分进程启动，不绑定桌面端。"""

from __future__ import annotations

import argparse

from autocoding_api.config import load_config


def arguments(worker: bool = False):
    parser = argparse.ArgumentParser(
        description="AutoCoding API Worker" if worker else "AutoCoding API"
    )
    parser.add_argument("--config", required=True, help="服务器 JSON 配置路径")
    if not worker:
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (ValueError, OSError):
        # Pydantic 的原始异常可能包含输入的 Token，因此只打印固定错误。
        parser.error("配置无法读取或校验失败，请检查绝对目录、Token 和项目权限")
    return args, config


def serve() -> None:
    import uvicorn

    from autocoding_api.http import create_app

    args, config = arguments()
    uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1)


def work() -> None:
    from autocoding_api.worker import run_worker

    _, config = arguments(worker=True)
    try:
        run_worker(config)
    except KeyboardInterrupt:
        pass
