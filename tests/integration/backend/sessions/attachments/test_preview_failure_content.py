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

from app.container import AppContainer
from app.core.path_utils import workspace_context
from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.message import AttachmentRef
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure import attachment_content_service
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)


@pytest.mark.parametrize("media", ["image", "video"])
async def test_failed_preview_keeps_uri_through_execution_and_cold_history(
    production_fork_container: AppContainer, fork_execution_message, inprocess_model_replay,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, media: str,
) -> None:
    container = production_fork_container
    replay, _controller = inprocess_model_replay
    session = await container.session_service.create(SessionCreateRequest(title="附件预览故障"))
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
    attachment = AttachmentRef(
        file_id=f"inline:preview.{media}", name=f"preview.{media}", content_type=content_type,
        data_url=f"data:{content_type};base64," + base64.b64encode(body).decode("ascii"),
    )
    with caplog.at_level(logging.ERROR, logger="app.services.business.user_content_builder"):
        accepted = await fork_execution_message(container, session.session_id, content="请检查附件", attachments=[attachment])
    saver = container.checkpointer
    frozen = saver.accepted_user_message(
        session.session_id, thread_id=accepted.thread_id,
        job_id=accepted.job_id, message_id=accepted.message_id,
    )
    assert json.dumps(private)[1:-1] not in frozen.model_dump_json()
    (resolved,) = frozen.response_metadata["attachments"]
    uri = resolved["file_id"]
    assert uri.startswith("boxteam://") and "/attachments/" in uri
    assert isinstance(frozen.content, list)
    assert not any(block.get("type") == "image_url" for block in frozen.content)
    (manifest,) = [block for block in frozen.content if block.get("metadata", {}).get("kind") == "attachment_manifest"]
    detail = ElementTree.fromstring(manifest["text"] + "</attachment>").attrib["preview_error"]
    assert f"file_id={uri}" in detail and f"stage={media}_preview" in detail
    assert f"error={'PermissionError' if media == 'image' else 'RuntimeError'}" in detail
    if media == "image":
        assert f"errno={errno.EACCES}" in detail
    assert manifest["metadata"]["preview_status"] == "failed" and uri in manifest["text"]
    assert "preview_error=" in manifest["text"] and detail in manifest["text"]
    logs = [record for record in caplog.records if record.name == "app.services.business.user_content_builder"]
    (log,) = logs
    assert log.exc_info and private in str(log.exc_info[1])
    if media == "image":
        assert failures == [log.exc_info[1]] and log.exc_info[1].filename == private
    assert log.exc_text and private in log.exc_text

    calls = [json.loads(path.read_text()) for path in replay.artifacts.glob("*.json")]
    (call,) = [entry for entry in calls if entry["identity"]["session_id"] == session.session_id]
    with saver._storage.storage.read_connection(session.session_id, accepted.thread_id) as connection:
        (assembly_id,) = connection.execute(
            "SELECT assembly_id FROM model_calls WHERE model_call_id=?", (call["identity"]["model_call_id"],),
        ).fetchone()
    assembly = saver._storage.assemblies.get_context_assembly(
        session.session_id, accepted.thread_id, assembly_id=assembly_id,
    )
    assembly.validate_hashes()
    assert assembly.request_hash_preimage == normalize_wire_request(call["request"])
    assert uri in json.dumps(call["request"]) and detail in json.dumps(call["request"], ensure_ascii=False)
    originals = saver.read_canonical_items(session.session_id, accepted.thread_id)
    # 冷 Saver 只从已提交历史取正文与 assembly，不重新读取预览或当前配置。
    cold = RolloutCheckpointSaver(container.session_service.path_resolver.sessions_root, attachment_store=container.attachment_blob_store)
    with workspace_context(container.workspace_id, container.workspace_root):
        cold_message = cold.accepted_user_message(
            session.session_id, thread_id=accepted.thread_id, job_id=accepted.job_id, message_id=accepted.message_id,
        )
        cold_items = cold.read_canonical_items(session.session_id, accepted.thread_id)
        cold_assembly = cold._storage.assemblies.get_context_assembly(
            session.session_id, accepted.thread_id, assembly_id=assembly_id,
        )
    assert cold_message == frozen and cold_items == originals and cold_assembly == assembly
    public = json.dumps({
        "message": cold_message.model_dump(mode="json"),
        "items": [item.to_dict() for item in cold_items],
        "assembly": cold_assembly.to_dict(), "request": call["request"],
    }, ensure_ascii=False)
    assert json.dumps(private)[1:-1] not in public and str(container.workspace_root) not in public
    assert container.attachment_blob_store.read(session.session_id, uri).data == body
