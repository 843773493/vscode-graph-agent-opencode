"""附件内容服务的有界等待回归。

视频抽帧走 subprocess.run，且该路径在事件循环线程内同步执行；若子进程无界
阻塞，任务连同取消会一起卡死。这里断言抽帧调用始终携带正的 timeout。
"""

from __future__ import annotations

import subprocess

import pytest

from app.services.infrastructure import attachment_content_service as svc


def test_video_frame_extraction_passes_bounded_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
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

