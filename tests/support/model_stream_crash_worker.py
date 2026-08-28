"""在独立进程中运行一个可被测试强制终止的 Provider 消费者。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Literal

import httpx
import litellm
import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler
from langchain_core.messages import AIMessageChunk, HumanMessage
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.session_paths import SessionPathResolver
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime

ProviderKind = Literal["chat", "responses"]


class _ReadyDeltaSink:
    """在首个 delta 完成持久化后写入跨进程就绪标记。"""

    def __init__(self, runtime: MessageStreamRuntime, ready_path: Path) -> None:
        self._runtime = runtime
        self._ready_path = ready_path
        self._marked = False

    async def accept_message_chunk(self, chunk: AIMessageChunk) -> None:
        await self._runtime.accept_message_chunk(chunk)
        if not self._marked:
            self._ready_path.parent.mkdir(parents=True, exist_ok=True)
            self._ready_path.write_text("ready\n", encoding="utf-8")
            self._marked = True


def _model(provider: ProviderKind, endpoint: str):
    if provider == "chat":
        return BoxteamLiteLLMChatModel(
            model="openai/big-pickle",
            api_key="test-key",
            api_base=endpoint,
            custom_llm_provider="openai",
            streaming=True,
        )
    return BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_key="test-key",
        api_base=endpoint,
        custom_llm_provider="openai",
        responses_store=False,
        streaming=True,
    )


async def _run(args: argparse.Namespace) -> None:
    client = httpx.AsyncClient()
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("崩溃 worker 启动前 LiteLLM async session 已存在")
    injected_handler = AsyncHTTPHandler.__new__(AsyncHTTPHandler)
    injected_handler.timeout = None
    injected_handler.event_hooks = None
    injected_handler.client_alias = "boxteam-provider-crash-worker"
    injected_handler.client = client
    previous_factory = litellm_http_handler.get_async_httpx_client

    def get_injected_async_client(
        *_args: object,
        **_kwargs: object,
    ) -> AsyncHTTPHandler:
        return injected_handler

    litellm_http_handler.get_async_httpx_client = get_injected_async_client
    litellm.aclient_session = client

    resolver = SessionPathResolver(Path(args.sessions_root))
    resolver.initialize()
    store = MessageStreamStore(path_resolver=resolver)
    writer = await store.open(
        session_id=args.session_id,
        turn_id=args.turn_id,
        job_id=args.turn_id,
    )
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    scope_token = set_current_turn_execution_scope(scope)
    sink_token = set_current_model_delta_sink(
        _ReadyDeltaSink(runtime, Path(args.ready_path))
    )
    try:
        await runtime.start_model("model_crash_worker", args.provider)
        async for _chunk in _model(args.provider, args.endpoint)._astream(
            [HumanMessage(content="测试后端崩溃恢复")]
        ):
            pass
    finally:
        reset_current_model_delta_sink(sink_token)
        reset_current_turn_execution_scope(scope_token)
        await scope.close()
        litellm_http_handler.get_async_httpx_client = previous_factory
        if litellm.aclient_session is client:
            litellm.aclient_session = previous_session
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("chat", "responses"), required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--sessions-root", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--ready-path", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
