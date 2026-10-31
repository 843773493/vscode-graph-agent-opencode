"""预览故障经真实消息准入、执行和冷读后仍只以 URI 标识附件。"""

from __future__ import annotations

import base64
import errno
import json
import logging
import shutil
import subprocess
from io import BytesIO
from xml.etree import ElementTree

import pytest
from PIL import Image

from app.core.path_utils import workspace_context
from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.message import AttachmentRef, MessageRunAccepted
from app.services.business.job.service import JobService
from app.services.infrastructure import attachment_content_service
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 两轮执行共用真实应用 lifespan。
)
from tests.support.api_helpers import create_session
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 原预览异常触发真实 ENOTDIR。
)


@pytest.mark.parametrize("media", ["image", "video"])
@pytest.mark.parametrize("failed_log", [False, True], ids=["normal-log", "failed-log"])
@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_failed_preview_keeps_uri_through_execution_and_cold_history(
    source_case: RuntimeSourceCase,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, media: str,
    diagnostic_sink: DiagnosticFailure, failed_log: bool,
) -> None:
    case = source_case
    container = case.container
    jobs = container.job_service
    assert isinstance(jobs, JobService)
    session_id = await create_session(case.client, "附件预览故障")
    private = "/private/preview/cache/input.png" if media == "image" else r"C:\private\preview\ffmpeg.exe"
    failures: list[Exception] = []
    content_type = "image/png" if media == "image" else "video/mp4"
    if media == "image":
        buffer = BytesIO()
        Image.new("RGB", (8, 8), color=(30, 80, 180)).save(buffer, format="PNG")
        body = buffer.getvalue()

        def fail_thumbnail(*_args, **_kwargs):
            error = PermissionError(errno.EACCES, f"预览缓存拒绝 {private}", private)
            failures.append(error)
            raise error

        monkeypatch.setattr(container.attachment_blob_store, "read_thumbnail", fail_thumbnail)
    else:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            pytest.skip("视频程序故障场景需要已安装 ffmpeg")
        body = b"controlled video preview failure"
        original_run = subprocess.run

        def fail_preview_program(command, *args, **kwargs):
            # 只替换视频程序的外部响应；其余子进程及附件读写保持真实。
            if command[0] == ffmpeg and "-frames:v" in command:
                return subprocess.CompletedProcess(command, 1, "", f"预览程序拒绝 {private}")
            return original_run(command, *args, **kwargs)

        monkeypatch.setattr(attachment_content_service.subprocess, "run", fail_preview_program)
    if failed_log:
        # 先执行原预览读取或 ffmpeg 响应处理，只观察其原异常实例。
        service = attachment_content_service.AttachmentContentService
        method = "image_preview_data_url" if media == "image" else "video_preview_data_urls"
        original_preview = service.image_preview_data_url if media == "image" else service.video_preview_data_urls

        def observe_preview(owner: attachment_content_service.AttachmentContentService, reference: AttachmentRef) -> str | list[str]:
            try:
                return original_preview(owner, reference)
            except (OSError, RuntimeError, ValueError) as error:
                diagnostic_sink.primary = error
                raise

        monkeypatch.setattr(service, method, observe_preview)
    attachment = AttachmentRef(
        file_id=f"inline:preview.{media}", name=f"preview.{media}", content_type=content_type,
        data_url=f"data:{content_type};base64," + base64.b64encode(body).decode("ascii"),
    )
    first_call, = await case.plan(session_id, [{"content": "已检查附件。"}])
    with caplog.at_level(logging.ERROR, logger="app.core.failures"):
        response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
            "message": {"content": "请检查附件", "attachments": [attachment.model_dump(mode="json")]},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        accepted = MessageRunAccepted.model_validate(response.json()["data"])
        await _job(case, accepted.job_id)
        await jobs.drain_finalizations()
    saver = container.checkpointer
    frozen = saver.accepted_user_message(
        session_id, thread_id=accepted.thread_id,
        job_id=accepted.job_id, message_id=accepted.message_id,
    )
    assert json.dumps(private)[1:-1] not in frozen.model_dump_json()
    (resolved,) = frozen.response_metadata["attachments"]
    uri = resolved["file_id"]
    assert uri.startswith("boxteam://") and "/attachments/" in uri
    assert isinstance(frozen.content, list)
    blocks = [block for block in frozen.content if isinstance(block, dict)]
    assert len(blocks) == len(frozen.content)
    assert not any(block.get("type") == "image_url" for block in blocks)
    (manifest,) = [block for block in blocks if block.get("metadata", {}).get("kind") == "attachment_manifest"]
    detail = ElementTree.fromstring(manifest["text"] + "</attachment>").attrib["preview_error"]
    assert f"file_id={uri}" in detail and f"stage={media}_preview" in detail
    assert f"error={'PermissionError' if media == 'image' else 'RuntimeError'}" in detail
    if media == "image":
        assert f"errno={errno.EACCES}" in detail
    assert manifest["metadata"]["preview_status"] == "failed" and uri in manifest["text"]
    assert "preview_error=" in manifest["text"] and detail in manifest["text"]
    logs = [record for record in caplog.records if record.exc_info and private in str(record.exc_info[1])]
    (log,) = logs
    assert log.exc_info and private in str(log.exc_info[1])
    primary = log.exc_info[1]
    assert isinstance(primary, BaseException)
    if media == "image":
        assert isinstance(primary, PermissionError)
        assert failures == [primary] and primary.filename == private
    assert log.exc_text and private in log.exc_text
    if failed_log:
        assert detail.endswith(", diagnostic_sink_error=NotADirectoryError")
        assert diagnostic_sink.primary is primary
        assert diagnostic_sink.errors and all(error.errno == errno.ENOTDIR for error in diagnostic_sink.errors)
        assert any(str(diagnostic_sink.errors[0]) in note for note in primary.__notes__)
    else:
        assert not diagnostic_sink.errors and "diagnostic_sink_error" not in detail

    call = {"identity": case.planned_calls[first_call], "request": case.request(first_call)}
    assert case.call_count(session_id, accepted.thread_id) == 1
    with saver._storage.storage.read_connection(session_id, accepted.thread_id) as connection:
        (assembly_id,) = connection.execute(
            "SELECT assembly_id FROM model_calls WHERE model_call_id=?", (call["identity"]["model_call_id"],),
        ).fetchone()
    assembly = saver._storage.assemblies.get_context_assembly(
        session_id, accepted.thread_id, assembly_id=assembly_id,
    )
    assembly.validate_hashes()
    assert assembly.request_hash_preimage == normalize_wire_request(call["request"])
    assert uri in json.dumps(call["request"]) and detail in json.dumps(call["request"], ensure_ascii=False)
    originals = saver.read_canonical_items(session_id, accepted.thread_id)
    # 冷 Saver 只从已提交历史取正文与 assembly，不重新读取预览或当前配置。
    cold = RolloutCheckpointSaver(container.session_service.path_resolver.sessions_root, attachment_store=container.attachment_blob_store)
    with workspace_context(container.workspace_id, container.workspace_root):
        cold_message = cold.accepted_user_message(
            session_id, thread_id=accepted.thread_id, job_id=accepted.job_id, message_id=accepted.message_id,
        )
        cold_items = cold.read_canonical_items(session_id, accepted.thread_id)
        cold_assembly = cold._storage.assemblies.get_context_assembly(
            session_id, accepted.thread_id, assembly_id=assembly_id,
        )
    assert cold_message == frozen and cold_items == originals and cold_assembly == assembly
    public = json.dumps({
        "message": cold_message.model_dump(mode="json"),
        "items": [item.to_dict() for item in cold_items],
        "assembly": cold_assembly.to_dict(), "request": call["request"],
    }, ensure_ascii=False)
    assert json.dumps(private)[1:-1] not in public and str(container.workspace_root) not in public
    assert "diagnostic-log-secret" not in public
    original_file = await case.client.get("/api/v1/workspace/files/raw", params={"path": uri})
    assert original_file.status_code == 200, original_file.text
    assert original_file.content == body
    if failed_log:
        # 后继输入仍走同一产品入口；不补写旧消息、assembly 或预览结果。
        diagnostic_sink.primary = None
        second_call, = await case.plan(session_id, [{"content": "已保留原预览失败事实。"}])
        successor = MessageRunAccepted.model_validate(await _send(case, session_id, "继续读取已保留的附件事实"))
        await _job(case, successor.job_id)
        await jobs.drain_finalizations()
        assert case.call_count(session_id, accepted.thread_id) == 2
        assert successor.thread_id == accepted.thread_id and successor.job_id != accepted.job_id
        assert saver.accepted_user_message(
            session_id, thread_id=accepted.thread_id, job_id=accepted.job_id, message_id=accepted.message_id,
        ) == frozen
        assert saver.read_canonical_items(session_id, accepted.thread_id)[:len(originals)] == originals
        assemblies = saver._storage.assemblies.list_context_assemblies(session_id, accepted.thread_id)
        assert {entry.model_call_id for entry in assemblies} == {first_call, second_call}
        for entry in assemblies:
            assert entry.model_call_id is not None
            assert entry.request_hash_preimage == normalize_wire_request(case.request(entry.model_call_id))
            entry.validate_hashes()
        assert next(entry for entry in assemblies if entry.model_call_id == first_call) == assembly
        successor_request = json.dumps(case.request(second_call), ensure_ascii=False)
        assert uri in successor_request and detail in successor_request
        assert json.dumps(private)[1:-1] not in successor_request and "diagnostic-log-secret" not in successor_request
