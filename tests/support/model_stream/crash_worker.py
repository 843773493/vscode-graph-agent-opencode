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
from langgraph.checkpoint.base import empty_checkpoint
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.providers.wire_finalize import (
    chat_wire_params,
    chat_wire_profile,
    responses_wire_params,
    responses_wire_profile,
)
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_thread_context import session_thread_owner_scope
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.model_stream.frozen_provider import create_frozen_provider_activation
from tests.support.session_catalog_owner import fixture_catalog_scope

ProviderKind = Literal["chat", "responses"]


class _ReadyDeltaSink:
    """在首个 delta 完成持久化后写入跨进程就绪标记。"""

    def __init__(self, runtime: MessageStreamRuntime, ready_path: Path) -> None:
        self._runtime = runtime
        self._ready_path = ready_path
        self._marked = False

    async def accept_message_chunk(
        self,
        chunk: AIMessageChunk,
        *,
        model_call_id: str | None = None,
    ) -> None:
        await self._runtime.accept_message_chunk(
            chunk,
            model_call_id=model_call_id,
        )
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


async def _run_with_catalog_owner(args: argparse.Namespace) -> None:
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

    sessions_root = Path(args.sessions_root)
    seed_catalog_session_bundle(sessions_root, args.session_id)
    resolver = get_session_path_resolver(sessions_root)
    if not isinstance(resolver, SessionCatalogPathResolver):
        raise TypeError(
            "崩溃 worker 要求 SQLite catalog resolver: "
            f"sessions_root={args.sessions_root}"
        )
    resolver.initialize()
    store = MessageStreamStore(path_resolver=resolver)
    thread_id = store._path_resolver.main_thread_id(args.session_id)
    saver = RolloutCheckpointSaver(sessions_root)
    job_id = f"job_{args.turn_id}"
    ingress_id = create_uuid_hex()
    accepted = saver.accept_turn(
        args.session_id,
        thread_id=thread_id,
        accepted_ingress_id=ingress_id,
        acceptance_idempotency_key=f"{args.turn_id}:accept",
        job_id=job_id,
        admission_idempotency_key=f"{args.turn_id}:admission",
        payload="测试后端崩溃恢复",
        turn_id=args.turn_id,
        root_item_id=f"item-{ingress_id}",
    )
    admission = saver.execution_admission(
        args.session_id,
        thread_id=thread_id,
        execution_id=str(accepted["initial_execution_id"]),
    )
    owner = MutationIntentOwner(args.session_id, thread_id)
    ingress_message = HumanMessage(content="测试后端崩溃恢复", id=ingress_id)
    ingress_message.response_metadata["turn_id"] = args.turn_id
    writer = await store.open(
        thread_id=thread_id,
        session_id=args.session_id,
        turn_id=str(accepted["turn_id"]),
        job_id=job_id,
    )
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    scope_token = set_current_turn_execution_scope(scope)
    sink_token = set_current_model_delta_sink(
        _ReadyDeltaSink(runtime, Path(args.ready_path))
    )
    try:
        model = _model(args.provider, args.endpoint)
        is_responses = args.provider == "responses"
        profile = responses_wire_profile(model) if is_responses else chat_wire_profile(model)
        wire_params = (
            responses_wire_params(model, kwargs={})
            if is_responses
            else chat_wire_params(model, stop=None, kwargs={}, stream=True)
        )
        tool_set = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": [ingress_message]}
        checkpoint["channel_versions"] = {"messages": "1"}
        plan_key = f"{args.turn_id}:assembly"
        activation_factory = create_frozen_provider_activation()
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            saver.put(
                build_checkpoint_config(thread_id),
                checkpoint,
                {"source": "loop", "step": 1, "parents": {}},
                {"messages": "1"},
            )
            activation_arguments = activation_factory(
                saver,
                plan_creation_idempotency_key=plan_key,
            )
            model_call_id = activation_arguments["model_call_id"]
            prepared = saver.prepare_context_for_provider(
                args.session_id,
                thread_id,
                turn_id=str(accepted["turn_id"]),
                execution_id=admission.execution_id,
                plan_creation_idempotency_key=plan_key,
                seal_idempotency_key=f"{plan_key}:seal",
                request_messages=[ingress_message],
                context_source_manager=ContextSourceManager(
                    owner=ContextSourceOwnerKey(args.session_id, thread_id),
                    control_state_port=saver,
                ),
                tool_set_supplier=lambda: tool_set,
                provider_version=profile.identity(),
                wire_profile=profile,
                wire_params=wire_params,
                target_format="responses" if is_responses else "chat_completions",
                **activation_arguments,
            )
            snapshot = prepared["snapshot"]
            assert saver.get_context_assembly(
                args.session_id,
                thread_id,
                assembly_id=snapshot.assembly_id,
            ) == snapshot
            assert snapshot.model_call_id == model_call_id
            native_projection = (
                saver.project_context_plan_to_native(
                    args.session_id,
                    thread_id,
                    prepared["plan"],
                )
                if is_responses
                else None
            )
            await runtime.start_model(model_call_id, args.provider)

            async def consume_prepared_request() -> None:
                with (
                    session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
                    execution_admission_scope(admission, langgraph_thread_id=thread_id),
                    sealed_assembly_dispatch_scope(
                        prepared["dispatch_ref"], snapshot,
                        resource_refs=prepared["resource_refs"], native_projection=native_projection,
                    ),
                ):
                    async for _chunk in model.astream(
                        prepared["messages"],
                        tools=prepared["tools"] or None,
                    ):
                        pass

            await consume_prepared_request()
    finally:
        reset_current_model_delta_sink(sink_token)
        reset_current_turn_execution_scope(scope_token)
        await scope.close()
        litellm_http_handler.get_async_httpx_client = previous_factory
        if litellm.aclient_session is client:
            litellm.aclient_session = previous_session
        await client.aclose()


async def _run(args: argparse.Namespace) -> None:
    with fixture_catalog_scope():
        await _run_with_catalog_owner(args)


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
