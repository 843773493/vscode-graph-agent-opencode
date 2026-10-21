"""附件内容服务的有界等待回归。

视频抽帧走 subprocess.run，且该路径在事件循环线程内同步执行；若子进程无界
阻塞，任务连同取消会一起卡死。这里断言抽帧调用始终携带正的 timeout。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.exceptions import ForbiddenError
from app.schemas.internal_v2.attachment import AttachmentRef
from app.services.infrastructure import attachment_content_service as svc


def test_video_frame_extraction_passes_bounded_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        captured["command"] = command
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="fake")

    monkeypatch.setattr(
        svc.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )
    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError):
        svc._extract_video_frame_data_urls(
            video_bytes=b"fake",
            content_type="video/mp4",
            attachment_name="clip.mp4",
        )

    assert "timeout" in captured, "视频抽帧子进程必须带 timeout，否则无界等待会卡死事件循环"
    assert captured["timeout"] == svc.VIDEO_FRAME_TIMEOUT_SECONDS
    assert captured["cwd"].is_absolute()
    command = captured["command"]
    assert not Path(command[command.index("-i") + 1]).is_absolute()
    assert not Path(command[-1]).is_absolute()


def test_attachment_rejects_outside_absolute_path_without_workspace_redirection(
    empty_workspace_scope, tmp_path,
):
    root = empty_workspace_scope[1]
    outside = tmp_path / "outside.txt"
    outside.write_text("原始目标")
    # 错误的 lstrip 回退会命中此工作区内的不同内容。
    decoy = root / str(outside).lstrip("/")
    decoy.parent.mkdir(parents=True)
    decoy.write_text("错误目标")
    service = svc.AttachmentContentService(workspace_root=root)
    with pytest.raises(ForbiddenError, match="超出") as failure:
        service.resolve_reference(AttachmentRef(file_id=str(outside)))
    assert str(root) not in str(failure.value)
    assert outside.read_text() == "原始目标"
    assert decoy.read_text() == "错误目标"


def test_attachment_relative_path_uses_explicit_target_and_missing_error_uses_uri(
    empty_workspace_scope, tmp_path, monkeypatch,
):
    root = empty_workspace_scope[1]
    (root / "same.txt").write_text("目标工作区")
    (tmp_path / "same.txt").write_text("进程目录")
    monkeypatch.chdir(tmp_path)
    service = svc.AttachmentContentService(workspace_root=root)
    reference = service.resolve_reference(AttachmentRef(file_id="same.txt"))
    assert service._read_workspace_attachment(reference) == "目标工作区".encode()
    with pytest.raises(FileNotFoundError) as failure:
        service.resolve_reference(AttachmentRef(file_id=str(root / "missing.txt")))
    assert "boxteam://" in str(failure.value)
    assert str(root) not in str(failure.value)

