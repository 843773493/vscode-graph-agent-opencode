from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import CompositeBackend
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config

from app.agents.deep_agent_stack import (
    _build_summarization_middleware,
    _filter_middleware_tools,
)
from app.agents.summarization.middleware import CachePreservingSummarizationMiddleware
from app.agents.summarization.tool_middleware import (
    CachePreservingSummarizationToolMiddleware,
)
from app.agents.workspace_backend import build_workspace_backend
from app.core.path_utils import workspace_context
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


class _ArchivePreflight:
    """本用例只核归档 owner；消息均为闭合的纯文本轮次。"""

    def safe_compaction_prefix_cutoffs(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        state_messages: Sequence[object],
        cutoff_indexes: Sequence[int],
    ) -> frozenset[int]:
        return frozenset(cutoff_indexes)


@dataclass(frozen=True)
class _HistoryArchiveCase:
    backend: CompositeBackend
    session_dir: Path
    workspace_root: Path
    workspace_id: str
    owners: tuple[MutationIntentOwner, MutationIntentOwner]


@pytest.fixture
def archive_case(tmp_path: Path) -> _HistoryArchiveCase:
    session_id = "ses_019c58e6e9f5713785f99a3f3e5768d8"
    main_thread_id = "thr_019c58e6e9f5713785f99a3f3e5768d8"
    child_thread_id = "thr_019c58e6e9f5713785f99a3f3e5768d9"
    bundle = seed_catalog_session_bundle(
        tmp_path / ".boxteam" / "sessions",
        session_id,
        main_thread_id=main_thread_id,
    )
    return _HistoryArchiveCase(
        build_workspace_backend(tmp_path),
        bundle.directory,
        tmp_path,
        bundle.node.workspace_id,
        (
            MutationIntentOwner(session_id, main_thread_id),
            MutationIntentOwner(session_id, child_thread_id),
        ),
    )


@pytest.fixture
def archive_scope(archive_case: _HistoryArchiveCase) -> Callable[[MutationIntentOwner], AbstractContextManager[None]]:
    @contextmanager
    def bind(owner: MutationIntentOwner) -> Iterator[None]:
        token = var_child_runnable_config.set(
            {"configurable": {"thread_id": owner.thread_id}}
        )
        try:
            with (
                workspace_context(archive_case.workspace_id, archive_case.workspace_root),
                session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
            ):
                yield
        finally:
            var_child_runnable_config.reset(token)

    return bind


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
async def test_compaction_archives_and_appends_by_session_thread_owner(
    archive_case: _HistoryArchiveCase,
    archive_scope: Callable[[MutationIntentOwner], AbstractContextManager[None]],
    use_async: bool,
) -> None:
    middleware = CachePreservingSummarizationMiddleware(
        model=FakeListChatModel(responses=["unused"]),
        backend=archive_case.backend,
        trigger=("messages", 4),
        keep=("messages", 2),
        token_counter=lambda messages, tools=None: sum(len(message.text) for message in messages),
        compaction_preflight=_ArchivePreflight(),
    )
    history_tool = next(tool for tool in CachePreservingSummarizationToolMiddleware(middleware).tools
                        if tool.name == "read_compaction_history")
    observed_requests: list[ModelRequest] = []

    def handler(request: ModelRequest) -> ModelResponse:
        observed_requests.append(request)
        return ModelResponse(result=[AIMessage(content="归档后的摘要")])

    async def async_handler(request: ModelRequest) -> ModelResponse:
        return handler(request)

    for owner_index, owner in enumerate(archive_case.owners):
        own_marker = f"owner-{owner_index}"
        for round_index in range(2):
            messages: list[AnyMessage] = []
            for pair_index in range(5):
                messages.extend([
                    HumanMessage(content=f"{own_marker}-round-{round_index}-pair-{pair_index} " * 60),
                    AIMessage(content="需要保留原文的回答 " * 60),
                ])
            request = ModelRequest(
                model=None,
                messages=messages,
                state={},
                runtime=SimpleNamespace(
                    execution_info=SimpleNamespace(
                        thread_id=owner.thread_id, checkpoint_ns="",
                        checkpoint_id=f"archive-checkpoint-{owner_index}-{round_index}",
                        task_id=f"archive-task-{owner_index}-{round_index}", node_attempt=1,
                    )
                ),
            )
            observed_requests.clear()
            with archive_scope(owner):
                if use_async:
                    result = await middleware._awrap_model_call_impl(request, async_handler)
                else:
                    result = middleware.wrap_model_call(request, handler)
                history_args = {"runtime": ToolRuntime(state={}, context=None, config={},
                    stream_writer=lambda _: None, tool_call_id="read-after-compaction", store=None),
                    "line_offset": 1, "limit": 2000}
                archive_text = await history_tool.ainvoke(history_args) if use_async else history_tool.invoke(history_args)
                assert f"{own_marker}-round-{round_index}" in archive_text
                assert f"owner-{1 - owner_index}" not in archive_text

            assert isinstance(result, ExtendedModelResponse)
            event = result.command.update["_summarization_event"]
            assert "file_path" not in event
            assert "read_compaction_history(line_offset=1, limit=200)" in event["summary_message"].text
            assert "session-artifacts/" not in event["summary_message"].text
            assert "history.md" not in event["summary_message"].text
            assert result.model_response.result == [] and len(observed_requests) == 1
            assert middleware.after_model(result.command.update, request.runtime) == {
                "_compaction_continuation": False, "jump_to": "model",
            }
            archive = archive_case.session_dir / "context" / owner.thread_id / "history.md"
            content = archive.read_text(encoding="utf-8")
            assert content.count("## Summarized at") == round_index + 1
            assert f"{own_marker}-round-0" in content
            assert f"{own_marker}-round-{round_index}" in content
            assert f"owner-{1 - owner_index}" not in content
            assert not (archive_case.session_dir / "context" / "history.md").exists()
            assert not (archive_case.workspace_root / "session-artifacts").exists()


@pytest.mark.parametrize(
    ("owner_state", "expected_error"),
    [
        ("missing_owner", "缺少显式 SessionThread owner"),
        ("mismatched_thread", "与 LangGraph callback thread_id 冲突"),
        ("missing_thread", "压缩历史归档缺少 LangGraph thread_id"),
    ],
)
def test_archive_path_rejects_missing_or_conflicting_owner(
    archive_case: _HistoryArchiveCase,
    owner_state: str,
    expected_error: str,
) -> None:
    main, child = archive_case.owners
    config_thread = child.thread_id if owner_state == "mismatched_thread" else main.thread_id
    config = {"configurable": {} if owner_state == "missing_thread" else {"thread_id": config_thread}}
    token = var_child_runnable_config.set(config)
    middleware = object.__new__(CachePreservingSummarizationMiddleware)
    try:
        with pytest.raises(RuntimeError, match=expected_error):
            if owner_state == "missing_owner":
                middleware._get_history_path()
            else:
                with session_thread_owner_scope(main, langgraph_thread_id=main.thread_id):
                    middleware._get_history_path()
    finally:
        var_child_runnable_config.reset(token)
    assert not (archive_case.session_dir / "context").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
async def test_history_tool_reads_original_archive_in_bound_thread_with_line_paging(
    archive_case: _HistoryArchiveCase,
    archive_scope: Callable[[MutationIntentOwner], AbstractContextManager[None]],
    use_async: bool,
) -> None:
    middleware = CachePreservingSummarizationMiddleware(
        model=FakeListChatModel(responses=["unused"]),
        backend=archive_case.backend,
        compaction_preflight=_ArchivePreflight(),
    )
    tools = CachePreservingSummarizationToolMiddleware(middleware).tools
    tool = next(tool for tool in tools if tool.name == "read_compaction_history")
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"line_offset", "limit"}
    runtime = ToolRuntime(state={}, context=None, config={}, stream_writer=lambda _: None, tool_call_id="history-read", store=None)
    for index, owner in enumerate(archive_case.owners):
        with archive_scope(owner):
            path = middleware._get_history_path()
            result = archive_case.backend.write(path, f"user-{index}\nassistant-{index}\n第三行\n")
            assert result.error is None
            first = {"runtime": runtime, "line_offset": 1, "limit": 1}
            rest = {"runtime": runtime, "line_offset": 2, "limit": 2}
            first_text = await tool.ainvoke(first) if use_async else tool.invoke(first)
            rest_text = await tool.ainvoke(rest) if use_async else tool.invoke(rest)
        assert f"user-{index}" in first_text and "assistant-" not in first_text
        assert f"assistant-{index}" in rest_text and "第三行" in rest_text
        assert f"user-{1-index}" not in first_text + rest_text
        assert "session-artifacts/" not in first_text + rest_text


def test_auto_compaction_also_exposes_history_tool_when_manual_compaction_is_disabled(
    archive_case: _HistoryArchiveCase,
) -> None:
    middleware = _build_summarization_middleware(
        FakeListChatModel(responses=["unused"]), archive_case.backend,
        compaction_preflight=_ArchivePreflight(),
    )
    for item in middleware:
        _filter_middleware_tools(item, {"compact_conversation"})
    names = {tool.name for item in middleware for tool in getattr(item, "tools", [])}
    assert names == {"read_compaction_history"}


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
async def test_thread_without_archive_fails_without_reading_source_or_leaking_its_location(
    archive_case: _HistoryArchiveCase,
    archive_scope: Callable[[MutationIntentOwner], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
    use_async: bool,
) -> None:
    middleware = CachePreservingSummarizationMiddleware(
        model=FakeListChatModel(responses=["unused"]), backend=archive_case.backend,
        compaction_preflight=_ArchivePreflight(),
    )
    tool = next(tool for tool in CachePreservingSummarizationToolMiddleware(middleware).tools
                if tool.name == "read_compaction_history")
    main, child = archive_case.owners
    with archive_scope(main):
        assert archive_case.backend.write(middleware._get_history_path(), "来源线程的私有归档").error is None
    runtime = ToolRuntime(state={}, context=None, config={}, stream_writer=lambda _: None,
                          tool_call_id="missing-archive", store=None)
    with archive_scope(child):
        result = await tool.ainvoke({"runtime": runtime}) if use_async else tool.invoke({"runtime": runtime})
    assert "detail-unavailable" in result
    assert "来源线程的私有归档" not in result
    assert "session-artifacts" not in result and "history.md" not in result
    assert str(archive_case.session_dir) not in result
    assert "backend 读取失败" in caplog.text and "history.md" in caplog.text
