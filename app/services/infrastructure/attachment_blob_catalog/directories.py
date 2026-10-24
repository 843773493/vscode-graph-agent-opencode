"""附件目录原件的发布保护；物理路径仍由原 locator 核验。"""

from __future__ import annotations

from pathlib import Path

from app.core.atomic_fs import fsync_directory, fsync_file
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_file,
)


def protect_original(root: Path) -> None:
    """只在 live 目录发布前保护原件；冻结 capture 仍由原 owner 校验和清理。"""
    original = resolve_attachment_file(root, ("original",))
    for file in original.iterdir():
        if file.is_file():
            file.chmod(0o400)
            fsync_file(file)
    original.chmod(0o500)
    fsync_directory(original)
