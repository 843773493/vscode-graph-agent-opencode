"""文件效果验收专用装配：在真实发布或回执交付处退出、阻塞目标。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.services.infrastructure.workspace_files import text_mutation
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from tests.support.e2e_control.backend_app import app

__all__ = ["app"]

_publish = text_mutation.publish_file_content
_transfer = WorkspaceTextMutationOwner.execute_transfer


def _record(name: str, value: object) -> None:
    path = Path(os.environ["BOXTEAM_TEST_MUTATION_ARTIFACTS"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _fault_publish(path, *args, **options):
    if path.name == "partial-second.txt":
        raise OSError("受控第二文件发布失败")
    result = _publish(path, *args, **options)
    if path.name == "publish-exit-first.txt":
        _record("publish-exit.json", {"stage": "first_file_published"})
        os._exit(72)
    if path.name in {"cancel-first.txt", "timeout-first.txt"}:
        phase = path.name.split("-", 1)[0]
        _record(phase + "-entered.json", {"stage": "first_file_published"})
        release = Path(os.environ["BOXTEAM_TEST_MUTATION_ARTIFACTS"]) / (phase + "-release")
        while not release.is_file():
            time.sleep(0.01)
    return result


def _fault_transfer(owner, payload):
    result = _transfer(owner, payload)
    command = payload.command
    if "response-drop-first.txt" in getattr(command, "input", ""):
        _record("response-drop.json", result.model_dump(mode="json"))
        os._exit(73)
    for phase in ("cancel", "timeout"):
        if any(item.path.endswith("/" + phase + "-third.txt") for item in result.receipt.files):
            _record(phase + "-finished.json", result.model_dump(mode="json"))
    return result


text_mutation.publish_file_content = _fault_publish
WorkspaceTextMutationOwner.execute_transfer = _fault_transfer
