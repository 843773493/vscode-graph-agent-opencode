"""同步 worker 在取消后仍须结算，再释放产品调用方的准入。

异步收尾统一使用 SDK 的 ``run_cleanup_shielded``；本模块仅负责线程桥接，
保留原异常的身份、cause 与 notes，不重新实现异步生命周期。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_sync_shielded(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """同步 I/O 交给 worker；取消须等 worker 结算后再释放调用方的准入。"""
    from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded

    def invoke() -> tuple[T] | BaseException:
        try:
            return (operation(*args, **kwargs),)
        except BaseException as error:  # noqa: BLE001 - 仅搬运原异常，异步任务仍原样抛出。
            # 线程桥接会重建 TimeoutError 等异常；原实例须连同 cause、notes 一起交还。
            return error

    async def execute() -> T:
        outcome = await asyncio.to_thread(invoke)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome[0]

    worker = asyncio.create_task(execute())

    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        async def settle() -> None:
            await worker

        # 正常结果已证明worker结束；再等待收尾task会新增吞掉首次取消的窗口。
        await run_cleanup_shielded(settle)
        raise
