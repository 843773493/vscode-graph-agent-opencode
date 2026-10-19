"""用配置中的多个真实 reasoning provider 生成 128 Turn rollout fixture。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

PROJECT_ROOT = Path.cwd().resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.agent_factory import build_model_from_provider
from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.providers.wire_finalize import (
    chat_wire_params,
    chat_wire_profile,
    responses_wire_params,
    responses_wire_profile,
)
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.container import WorkspaceContainerRegistry
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope, get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.mapping.itemized.provider_history import visible_text
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from app.services.orchestration.resource_activation import ResourceActivationCoordinator

SESSION_ID = "ses_019c27f816b17ee394a67f9312bb669e"


def _load_running_provider_keys(environment_names: set[str]) -> None:
    """从当前工作区后端进程继承已加载的 provider key，不输出密钥。"""
    missing_names = {
        name for name in environment_names if not os.environ.get(name)
    }
    if not missing_names:
        return
    for process_path in Path("/proc").glob("[0-9]*"):
        environ_path = process_path / "environ"
        command_path = process_path / "cmdline"
        try:
            command = command_path.read_bytes().decode(errors="ignore")
            if (
                "vscode-graph-agent-opencode" not in command
                or "app.main" not in command
            ):
                continue
            entries = environ_path.read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError):
            continue
        for entry in entries:
            name_bytes, separator, value_bytes = entry.partition(b"=")
            if not separator:
                continue
            name = name_bytes.decode(errors="ignore")
            if name in missing_names and value_bytes:
                os.environ[name] = value_bytes.decode()
                missing_names.remove(name)
        if not missing_names:
            return


def _configured_environment_names(config_paths: tuple[Path, ...]) -> set[str]:
    """从 JSONC 配置中提取 provider 使用的环境变量名。"""
    names: set[str] = set()
    for config_path in config_paths:
        if not config_path.is_file():
            continue
        names.update(
            re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", config_path.read_text())
        )
    return names


def _update_fixture_configuration(
    resolver: SessionCatalogPathResolver,
    *,
    current_provider_id: str = "primary",
) -> None:
    """fixture 业务配置只写 catalog，身份 envelope 保持不可变。"""
    resolver.catalog_store.update_session_configuration(
        SESSION_ID, current_agent_id="default", current_provider_id=current_provider_id,
        updated_at=datetime.fromisoformat(resolver.catalog_store.get_node(SESSION_ID).updated_at),
    )


def _restore_generation_backup(workspace_root: Path) -> None:
    """恢复真实模型 fixture 的唯一未完成替换备份。"""
    registry = WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry([
            MountedWorkspace(load_or_create_workspace_id(workspace_root), workspace_root)
        ]),
        project_root=Path.cwd(),
    )
    with registry, catalog_provider_scope(registry):
        resolver = get_session_path_resolver(workspace_root / ".boxteam" / "sessions")
        session_dir = resolver.resolve_thread_node(
            SESSION_ID, resolver.main_thread_id(SESSION_ID)
        )
    backups = sorted(session_dir.glob(".rollout-backup-*/"))
    if not backups:
        return
    if len(backups) != 1:
        raise RuntimeError(f"真实模型 fixture 备份数量异常: {backups}")
    backup = backups[0]
    rollout_dir = session_dir / "rollout"
    failed_dir = session_dir / ".rollout-failed-regeneration"
    if failed_dir.exists():
        raise RuntimeError(f"真实模型 fixture 存在未清理失败目录: {failed_dir}")
    if rollout_dir.exists():
        rollout_dir.rename(failed_dir)
    backup.rename(rollout_dir)
    if failed_dir.exists():
        shutil.rmtree(failed_dir)


@tool("inspect_fixture")
def inspect_fixture(path: str, turn: int) -> str:
    """返回测试工作区中指定 fixture 的短结构化结果。"""
    return f"mock-tool-ok path={path} turn={turn} bytes={128 + turn}"


def _checkpoint(
    checkpoint_id: str, messages: list[object], provider_id: str, turn: int
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {
        "messages": messages,
        "model_provider": provider_id,
        "turn_count": turn,
    }
    checkpoint["channel_versions"] = {
        "messages": checkpoint_id,
        "model_provider": checkpoint_id,
        "turn_count": checkpoint_id,
    }
    checkpoint["updated_channels"] = ["messages", "model_provider", "turn_count"]
    checkpoint["versions_seen"] = {"agent": {"messages": checkpoint_id}}
    checkpoint["pending_sends"] = []
    return checkpoint


def _ensure_message_id(message: AIMessage, turn: int, suffix: str) -> AIMessage:
    if isinstance(message.id, str) and message.id:
        return message
    return message.model_copy(update={"id": f"model-{turn:03d}-{suffix}"})


def _stamp_actual_provider(
    message: AIMessage,
    provider_id: str,
    provider: dict[str, Any],
) -> AIMessage:
    """把实际响应 provider 写入消息，供后续跨模型上下文过滤使用。"""
    metadata = dict(message.response_metadata or {})
    metadata["provider_id"] = provider_id
    metadata["model_provider"] = provider_id
    metadata["model"] = provider.get("model")
    metadata["api_mode"] = parse_provider_api_mode(provider).protocol
    metadata["custom_llm_provider"] = provider.get("custom_llm_provider")
    return message.model_copy(update={"response_metadata": metadata})


def _provider_id_for_turn(turn: int, provider_ids: tuple[str, ...]) -> str:
    if not provider_ids:
        raise ValueError("真实模型 fixture 没有可用 provider")
    return provider_ids[(turn - 1) % len(provider_ids)]


def _invoke_fixture_model(
    model: BaseChatModel,
    *,
    saver: RolloutCheckpointSaver,
    admission: ExecutionAdmission,
    adapter: StepModelCallAdapter,
    provider_id: str,
    messages: list[BaseMessage],
    use_tools: bool,
    attempt: int,
) -> AIMessage:
    """生成器也先冻结实际请求，再经正式登记与 Provider 派发链调用模型。"""
    with (
        session_thread_owner_scope(
            MutationIntentOwner(admission.session_id, admission.thread_id),
            langgraph_thread_id=admission.thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=admission.thread_id),
    ):
        coordinator = ResourceActivationCoordinator(
            registry=ResourceRegistry(), saver=saver,
            body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
        )
        model_call_id = str(uuid4())

        async def freeze():
            parent = await coordinator.freeze_execution(
                admission=admission,
                policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}),
            )
            return await coordinator.commit_model_call(await coordinator.capture_model_call(
                parent=parent, model_call_id=model_call_id,
            ))

        activation = asyncio.run(freeze())
        schemas = [convert_to_openai_tool(inspect_fixture)] if use_tools else []
        tool_set = ToolSetSnapshot(selection_revision=sha256_jcs(schemas), tools=schemas)
        native = isinstance(model, BoxteamOpenAIResponsesModel)
        profile = responses_wire_profile(model) if native else chat_wire_profile(model)
        prepared = saver.prepare_context_for_provider(
            admission.session_id, admission.thread_id,
            turn_id=admission.turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key=f"real-request:{model_call_id}",
            seal_idempotency_key=f"real-seal:{model_call_id}",
            request_messages=messages,
            context_source_manager=ContextSourceManager(
                owner=ContextSourceOwnerKey(admission.session_id, admission.thread_id),
                control_state_port=saver,
            ),
            tool_set_supplier=lambda: tool_set,
            provider_version=profile.identity(), wire_profile=profile,
            wire_params=responses_wire_params(model) if native else chat_wire_params(model, stream=True),
            target_format="responses" if native else "chat_completions",
            model_call_id=model_call_id, activation_snapshot=activation,
        )
        asyncio.run(adapter.register(model_call_id, attempt, provider_id))
        try:
            with sealed_assembly_dispatch_scope(
                prepared["dispatch_ref"], prepared["snapshot"],
                native_projection=prepared.get("native_projection"),
            ):
                options = {"tools": prepared["tools"]} if prepared["tools"] else {}
                response = model.invoke(prepared["messages"], **options)
        except Exception:
            saver.update_model_call_outcome(
                admission.session_id, thread_id=admission.thread_id,
                model_call_id=model_call_id, outcome="failed", dispatch_state="failed",
            )
            raise
        saver.update_model_call_outcome(
            admission.session_id, thread_id=admission.thread_id,
            model_call_id=model_call_id, outcome="completed", dispatch_state="completed",
        )
        return response.model_copy(update={
            "response_metadata": {**response.response_metadata, "model_call_id": model_call_id},
        })


def _invoke_with_provider_fallback(
    invoke: Callable[[str, list[BaseMessage], bool], AIMessage],
    provider_ids: tuple[str, ...],
    requested_provider_id: str,
    messages: list[BaseMessage],
    *,
    provider_available: dict[str, bool],
    use_tools: bool,
    allow_fallback: bool = True,
    require_visible_output: bool = False,
) -> tuple[AIMessage, str]:
    """调用请求 provider；失败时按配置顺序切换到其它可用 provider。

    已失败的 provider 会在它再次成为轮换请求目标时重试，因此短暂限流
    恢复后仍能重新进入同一 rollout，而不会永久粘在某个 fallback 上。
    """

    ordered_provider_ids = (
        (requested_provider_id,)
        if not allow_fallback
        else (
            requested_provider_id,
            *(
                provider_id
                for provider_id in provider_ids
                if provider_id != requested_provider_id
            ),
        )
    )
    errors: list[Exception] = []
    for provider_id in ordered_provider_ids:
        # 当前轮次明确请求的 provider 即使之前失败也要重试；其它 provider
        # 在本轮只使用已知可用的候选，避免每个 Turn 重复撞限流。
        if provider_id != requested_provider_id and not provider_available.get(
            provider_id, True
        ):
            continue
        try:
            response = invoke(provider_id, messages, use_tools)
            if require_visible_output and not (
                response.tool_calls or visible_text(response.content).strip()
            ):
                raise RuntimeError(
                    "模型只返回内部 reasoning，没有用户可见正文或工具调用。"
                )
        except Exception as error:  # noqa: BLE001 - provider 异常必须触发候选切换
            provider_available[provider_id] = False
            error.add_note(f"fixture provider={provider_id} requested={requested_provider_id}")
            errors.append(error)
            print(
                "provider fallback: "
                f"requested={requested_provider_id} failed={provider_id} "
                f"error={type(error).__name__}"
            )
            continue
        provider_available[provider_id] = True
        return response, provider_id

    if not errors:
        raise RuntimeError(
            f"真实模型 fixture 没有可用 provider: requested={requested_provider_id}"
        )
    raise RuntimeError(
        f"真实模型 fixture 的 provider fallback 全部失败: requested={requested_provider_id}"
    ) from ExceptionGroup("真实模型 fixture 的各 Provider 原始失败", errors)


def generate(
    workspace_root: Path,
    *,
    start_turn: int = 1,
    end_turn: int = 128,
    tool_every: int = 8,
    require_mixed_provider: bool = False,
    require_reasoning_mix: bool = False,
    requested_provider_ids: tuple[str, ...] | None = None,
    requested_provider_sequence: tuple[str, ...] | None = None,
    strict_provider_sequence: bool = False,
) -> set[str]:
    if tool_every < 0:
        raise ValueError("tool_every 不能小于 0")
    inline_config_path = Path.cwd() / "configs" / "workspace_dev.jsonc"
    override_config_path = (
        workspace_root / ".boxteam" / "workspace_real_model_override.jsonc"
    )
    _load_running_provider_keys(
        _configured_environment_names((inline_config_path, override_config_path))
    )
    registry = WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry([
            MountedWorkspace(load_or_create_workspace_id(workspace_root), workspace_root)
        ]),
        project_root=Path.cwd(),
    )
    with registry, catalog_provider_scope(registry):
        sessions_dir = workspace_root / ".boxteam" / "sessions"
        resolver = get_session_path_resolver(sessions_dir)
        resolver.initialize()
        session_dir = resolver.resolve_session_node(SESSION_ID)
        thread_id = resolver.main_thread_id(SESSION_ID)
        rollout_dir = resolver.resolve_thread_node(SESSION_ID, thread_id) / "rollout"
        previous_rollout_backup: Path | None = None
        if start_turn == 1 and rollout_dir.exists():
            # 该脚本只替换明确指定的常驻真实模型会话，不触碰其它测试会话。
            if require_mixed_provider:
                previous_rollout_backup = session_dir / f".rollout-backup-{uuid4().hex}"
                shutil.copytree(rollout_dir, previous_rollout_backup)
            shutil.rmtree(rollout_dir)
        _update_fixture_configuration(resolver)

        config_service = ConfigService(
            workspace_root=workspace_root,
            inline_config_path=inline_config_path,
            config_path=override_config_path,
        )
        config_service.validate_workspace_config()
        runtime_config = config_service.get_agent_runtime_config("default")
        configured_providers = {
            str(provider["id"]): provider
            for provider in runtime_config["providers"]
            if isinstance(provider, dict) and isinstance(provider.get("id"), str)
        }
        configured_provider_ids = tuple(configured_providers)
        provider_ids = requested_provider_ids or requested_provider_sequence or configured_provider_ids
        if requested_provider_sequence:
            provider_ids = tuple(dict.fromkeys(requested_provider_sequence))
        unknown_provider_ids = sorted(set(provider_ids) - set(configured_provider_ids))
        if unknown_provider_ids:
            raise ValueError(f"请求的 provider 不在 default agent 配置中: {unknown_provider_ids}")
        if len(provider_ids) < 2:
            raise RuntimeError(
                "真实模型 fixture 至少需要两个配置 provider，才能验证模型切换"
            )
        models: dict[str, BaseChatModel] = {}
        provider_specs: dict[str, dict[str, Any]] = {}
        for provider_id in provider_ids:
            provider = configured_providers[provider_id]
            runtime = config_service.get_agent_runtime_config(
                "default",
                preferred_provider_id=provider_id,
            )
            models[provider_id] = build_model_from_provider(provider, runtime)
            provider_specs[provider_id] = provider

        provider_ids = tuple(models)
        if len(provider_ids) < 2:
            raise RuntimeError("真实模型 fixture 实际只能构建一个 provider，无法验证切换")

        saver = RolloutCheckpointSaver(sessions_dir)
        config = build_checkpoint_config(thread_id)
        messages: list[BaseMessage] = []
        last_provider_id = provider_ids[0]
        observed_provider_ids: set[str] = set()
        provider_available = {provider_id: True for provider_id in provider_ids}
        previous_request_provider: str | None = None
        for turn in range(start_turn, end_turn + 1):
            requested_provider_id = (
                requested_provider_sequence[(turn - 1) % len(requested_provider_sequence)]
                if requested_provider_sequence
                else _provider_id_for_turn(turn, provider_ids)
            )
            turn_id = f"real-turn-{turn:04d}"
            user = HumanMessage(
                id=f"real-user-{turn:04d}",
                content=(
                    f"这是第 {turn} 轮真实模型回归。本轮请求使用“{requested_provider_id}”模型；"
                    "如果上游发生模型切换，请明确说明实际使用的模型。请用简短中文回答，"
                    f"并总结上一轮上下文中的一个事实。第 {turn} 轮需要检查 fixture/{turn:04d}.json。"
                    + (
                        "请调用 inspect_fixture 工具后再回答。"
                        if tool_every > 0 and turn % tool_every == 0
                        else ""
                    )
                ),
                response_metadata={
                    "message_metadata": {"turn_id": turn_id},
                    "model_provider": requested_provider_id,
                },
            )
            accepted = saver.accept_turn(
                SESSION_ID,
                thread_id=thread_id,
                accepted_ingress_id=f"real-ingress-{turn:04d}",
                acceptance_idempotency_key=f"real-acceptance-{turn:04d}",
                job_id=f"real-job-{turn:04d}",
                admission_idempotency_key=f"real-admission-{turn:04d}",
                payload=user.content,
                turn_id=turn_id,
                root_item_id=f"item-{user.id}",
                initial_execution_id=f"real-execution-{turn:04d}",
            )
            admission = saver.execution_admission(
                SESSION_ID, thread_id=thread_id,
                execution_id=str(accepted["initial_execution_id"]),
            )
            adapter = StepModelCallAdapter(
                checkpointer=saver, session_id=SESSION_ID, thread_id=thread_id,
                job_id=admission.job_id, checkpoint_ns="",
            )
            model_call_attempt = 0

            def invoke(
                provider_id: str, request: list[BaseMessage], use_tools: bool, *,
                frozen_admission: ExecutionAdmission = admission,
                turn_adapter: StepModelCallAdapter = adapter,
                turn_number: int = turn,
            ) -> AIMessage:
                nonlocal model_call_attempt, config, previous_request_provider
                model_call_attempt += 1
                if previous_request_provider is not None and previous_request_provider != provider_id:
                    # 模型轮换明确创建真实 rewind view，保留完整对话前缀；
                    # 不给普通同 epoch append 伪造 profile 或 ToolSet 变更。
                    with (
                        session_thread_owner_scope(
                            MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id,
                        ),
                        execution_admission_scope(frozen_admission, langgraph_thread_id=thread_id),
                    ):
                        boundary = _checkpoint(
                            f"real-switch-{frozen_admission.turn_id}-{model_call_attempt}",
                            request, previous_request_provider, turn_number,
                        )
                        config = saver.put(
                            config, boundary, {"source": "real-model-provider-switch"},
                            boundary["channel_versions"],
                        )
                        config = saver.rewind(config, checkpoint_id=boundary["id"])
                previous_request_provider = provider_id
                return _invoke_fixture_model(
                    models[provider_id], saver=saver, admission=frozen_admission, adapter=turn_adapter,
                    provider_id=provider_id, messages=request, use_tools=use_tools,
                    attempt=model_call_attempt,
                )

            messages.append(user)
            response, provider_id = _invoke_with_provider_fallback(
                invoke,
                provider_ids,
                requested_provider_id,
                messages,
                provider_available=provider_available,
                use_tools=tool_every > 0 and turn % tool_every == 0,
                allow_fallback=not strict_provider_sequence,
                require_visible_output=True,
            )
            if strict_provider_sequence and provider_id != requested_provider_id:
                raise RuntimeError(
                    "真实模型 fixture 未按要求使用 provider: "
                    f"turn={turn} requested={requested_provider_id} actual={provider_id}"
                )
            last_provider_id = provider_id
            observed_provider_ids.add(provider_id)
            response = _ensure_message_id(response, turn, "tool-or-first")
            response = _stamp_actual_provider(
                response,
                provider_id,
                provider_specs[provider_id],
            )
            messages.append(response)
            if response.tool_calls:
                for call in response.tool_calls:
                    args = call.get("args")
                    if not isinstance(args, dict):
                        tool_result = (
                            "inspect_fixture 工具调用参数校验失败：参数必须是对象，"
                            f"实际类型为 {type(args).__name__}。"
                        )
                    else:
                        try:
                            tool_result = str(inspect_fixture.invoke(args))
                        except (TypeError, ValueError, ValidationError) as error:
                            tool_result = (
                                "inspect_fixture 工具调用参数校验失败："
                                f"{type(error).__name__}: {error}"
                            )
                            print(
                                "tool validation error: "
                                f"turn={turn} error={type(error).__name__}",
                                file=sys.stderr,
                            )
                    messages.append(
                        ToolMessage(
                            id=f"real-tool-result-{turn:04d}-{call['id']}",
                            content=str(tool_result),
                            name=str(call.get("name") or "inspect_fixture"),
                            tool_call_id=str(call["id"]),
                            response_metadata={
                                "message_metadata": {"turn_id": turn_id},
                                "model_provider": provider_id,
                            },
                        )
                    )
                final_response, final_provider_id = _invoke_with_provider_fallback(
                    invoke,
                    provider_ids,
                    provider_id,
                    messages,
                    provider_available=provider_available,
                    use_tools=False,
                    allow_fallback=not strict_provider_sequence,
                    require_visible_output=True,
                )
                if strict_provider_sequence and final_provider_id != requested_provider_id:
                    raise RuntimeError(
                        "真实模型 fixture 的工具后最终响应切换了 provider: "
                        f"turn={turn} requested={requested_provider_id} actual={final_provider_id}"
                    )
                provider_id = final_provider_id
                observed_provider_ids.add(provider_id)
                last_provider_id = provider_id
                final_response = _ensure_message_id(final_response, turn, "final")
                final_response = _stamp_actual_provider(
                    final_response,
                    provider_id,
                    provider_specs[provider_id],
                )
                messages.append(final_response)
            else:
                final_response = response
            with (
                session_thread_owner_scope(
                    MutationIntentOwner(SESSION_ID, thread_id),
                    langgraph_thread_id=thread_id,
                ),
                execution_admission_scope(admission, langgraph_thread_id=thread_id),
            ):
                adapter.converge_final_message(final_response)
                checkpoint = _checkpoint(f"real-checkpoint-{turn:04d}", messages, provider_id, turn)
                config = saver.put(
                    config,
                    checkpoint,
                    {
                        "source": "real-model-rollout-fixture",
                        "turn": turn,
                        "requested_provider_id": requested_provider_id,
                        "provider_id": provider_id,
                        "model": provider_specs[provider_id].get("model"),
                        "api_mode": parse_provider_api_mode(
                            provider_specs[provider_id]
                        ).protocol,
                        "model_switch": provider_id != requested_provider_id,
                    },
                    checkpoint["channel_versions"],
                )
                asyncio.run(adapter.converge_final_checkpoint(final_response.id))
            if turn % 8 == 0 or turn in {1, 64, 65, 96, 97, 128}:
                print(
                    "real rollout progress: "
                    f"turn={turn} requested={requested_provider_id} provider={provider_id}"
                )

        with sqlite3.connect(rollout_dir / "index.sqlite") as connection:
            reasoning_carriers = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT carrier_type FROM reasoning_blocks"
                )
            }
        if require_mixed_provider and len(observed_provider_ids) < 2:
            if previous_rollout_backup is not None:
                shutil.rmtree(rollout_dir, ignore_errors=True)
                os.replace(previous_rollout_backup, rollout_dir)
            raise RuntimeError(
                "真实模型 fixture 未同时成功使用两个 provider: "
                f"observed={sorted(observed_provider_ids)}"
            )
        if require_reasoning_mix and not {
            "reasoning",
            "summary",
            "encrypted",
        }.issubset(reasoning_carriers):
            if previous_rollout_backup is not None:
                shutil.rmtree(rollout_dir, ignore_errors=True)
                os.replace(previous_rollout_backup, rollout_dir)
            raise RuntimeError(
                "真实模型 fixture 未同时形成 reasoning、summary、encrypted reasoning: "
                f"carriers={sorted(reasoning_carriers)}"
            )
        if previous_rollout_backup is not None:
            shutil.rmtree(previous_rollout_backup)
        _update_fixture_configuration(
            resolver,
            current_provider_id=last_provider_id,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=(
            Path.cwd()
            / "tests"
            / "fixtures"
            / "workspaces"
            / "custom_tool_test_workspace"
        ),
    )
    parser.add_argument("--start-turn", type=int, default=1)
    parser.add_argument("--end-turn", type=int, default=128)
    parser.add_argument(
        "--tool-every",
        type=int,
        default=8,
        help="每隔多少轮绑定一次 inspect_fixture；设为 0 表示不绑定工具",
    )
    parser.add_argument(
        "--require-mixed-provider",
        action="store_true",
        help="至少两个实际 provider 成功产生响应时才算生成成功",
    )
    parser.add_argument(
        "--require-reasoning-mix",
        action="store_true",
        help="要求 fixture 同时包含 reasoning/summary 与 encrypted reasoning",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        help="可选 provider 白名单；默认使用 default agent 配置中的全部 provider",
    )
    parser.add_argument(
        "--provider-sequence",
        nargs="+",
        help="按 turn 循环使用的 provider 序列；允许显式重复同一个 provider",
    )
    parser.add_argument(
        "--strict-provider-sequence",
        action="store_true",
        help="provider 请求失败时不 fallback，并要求实际 provider 等于请求 provider",
    )
    args = parser.parse_args()
    workspace_root = args.workspace.resolve()
    try:
        generate(
            workspace_root,
            start_turn=args.start_turn,
            end_turn=args.end_turn,
            tool_every=args.tool_every,
            require_mixed_provider=args.require_mixed_provider,
            require_reasoning_mix=args.require_reasoning_mix,
            requested_provider_ids=tuple(args.providers) if args.providers else None,
            requested_provider_sequence=(
                tuple(args.provider_sequence) if args.provider_sequence else None
            ),
            strict_provider_sequence=args.strict_provider_sequence,
        )
    except Exception:
        _restore_generation_backup(workspace_root)
        raise


if __name__ == "__main__":
    main()
