"""真实提示词文件故障经执行准入、持久Job和HTTP返回同一无路径错误。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from app import main
from app.core.resource_uri import resource_display_uri
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobService
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    _plan_model_call,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 工作区和产物归本文件。
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.fixture
def failed_diagnostic_sink(monkeypatch: pytest.MonkeyPatch, request, caplog):
    # 正式临时路径沿本文件的run context，不写项目根或用户配置。
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    blocked = context.artifacts_dir / "blocked-log-parent"
    blocked.write_text("不是目录", encoding="utf-8")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if record.name == "app.core.failures":
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return blocked


@pytest.mark.parametrize("producer", ["R01", "R02"])
async def test_failed_file_blocks_new_execution_without_persisting_native_diagnostics(
    mounted_case: MountedCase, inprocess_model_replay, failed_diagnostic_sink: Path,
    monkeypatch: pytest.MonkeyPatch, caplog, producer: str,
) -> None:
    identity, root = next(reversed(mounted_case.roots.items()))
    source = root / "prompt.txt"
    source.write_text("已封存的提示词文件正文", encoding="utf-8")
    uri = resource_display_uri(
        scope="workspace", scope_id=identity, kind="files", tail_segments=("root", source.name),
    )
    config_path = root / ".boxteam/workspace.jsonc"
    instructions: dict[str, object] = (
        {"system_prompt_file": uri} if producer == "R01"
        else {"runtime_identity": {"description_file": uri}}
    )
    config_path.write_text(json.dumps({"agents": {"default": {"instructions": instructions}}}), encoding="utf-8")
    replay, controller = inprocess_model_replay
    async with main.lifespan(main.app):
        selected = next(item for item in mounted_case.containers if item.workspace_id == identity)
        config = selected.config_service
        jobs = selected.job_service
        assert isinstance(jobs, JobService)
        # 本场景验收显式reload→准入，不让独立watcher代做故障或恢复步骤。
        await config.stop_watching()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
            headers=LOCAL_TOKEN_HEADERS,
        ) as client:
            base = f"/api/v1/workspaces/{identity}"
            created = await client.post(base + "/sessions", json={"title": "提示词来源错误归属"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            thread = await client.get(base + f"/sessions/{session_id}/main-thread")
            assert thread.status_code == 200, thread.text
            thread_id = thread.json()["data"]["thread_id"]

            async def send():
                response = await client.post(base + f"/sessions/{session_id}/messages", json={
                    "message": {"content": "核对提示词来源"}, "run": {},
                })
                assert response.status_code == 200, response.text
                job = jobs._jobs[response.json()["data"]["job_id"]]
                assert job.task is not None
                await asyncio.wait_for(asyncio.shield(job.task), 30)
                await jobs.drain_finalizations()
                return job

            _plan_model_call(replay, selected, session_id, thread_id)
            first = await send()
            assert first.status == JobStatus.completed, first.error_message
            frozen = config.get_snapshot()
            saver = selected.checkpointer
            sealed = {
                item.assembly_id: canonical_json_bytes(item.to_dict())
                for item in saver.list_context_assemblies(session_id, thread_id)
            }
            assert sealed and controller.transport.call_count == 1
            inputs = config._instruction_inputs
            resolve = inputs._resolve_file
            assert resolve is not None
            saved = root / "original-prompt.txt"

            def replace_after_resolution(value: str):
                assert resolve is not None
                result = resolve(value)
                if value == uri:
                    source.rename(saved)
                    source.symlink_to(source.name)
                return result

            try:
                with monkeypatch.context() as fault:
                    fault.setattr(inputs, "_resolve_file", replace_after_resolution)
                    with pytest.raises(RuntimeError) as load_error:
                        await config.reload(idempotency_key="instruction-native-failure")
                original = load_error.value
                assert type(original) is RuntimeError and str(source) in str(original)
                assert config.get_snapshot().to_dict() == frozen.to_dict()
                failed = await send()
                assert failed.status == JobStatus.failed
                assert controller.transport.call_count == 1
                detail = failed.error_message
                assert detail is not None and "instruction-source-unavailable" in detail
                assert "stage=instruction-source-readiness" in detail and "error=RuntimeError" in detail
                assert "diagnostic_sink_error=NotADirectoryError" in detail and uri in detail
                assert str(root) not in detail and str(failed_diagnostic_sink) not in detail
                assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
                for endpoint in (f"/jobs/{failed.job_id}", "/config/reload-status", "/config"):
                    response = await client.get(base + endpoint)
                    assert response.status_code == 200, response.text
                    assert str(root) not in response.text and str(failed_diagnostic_sink) not in response.text
                    assert response.headers["X-Request-ID"] == response.json()["request_id"]
                source.unlink()
                saved.rename(source)
                source.write_text("由原文件入口恢复的新正文", encoding="utf-8")
                assert await config.reload(idempotency_key="instruction-native-recovered")
                config.require_instruction_sources_available("default")
                _plan_model_call(replay, selected, session_id, thread_id)
                recovered = await send()
                assert recovered.status == JobStatus.completed, recovered.error_message
                assert controller.transport.call_count == 2
                current = {
                    item.assembly_id: canonical_json_bytes(item.to_dict())
                    for item in saver.list_context_assemblies(session_id, thread_id)
                }
                assert {key: current[key] for key in sealed} == sealed
            finally:
                if source.is_symlink():
                    source.unlink()
                    saved.rename(source)
