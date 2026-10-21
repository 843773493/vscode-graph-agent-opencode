from pathlib import Path

import pytest

from app.gateway.runtime.local_workspace import validate_workspace_root_access


def test_validate_workspace_root_access_rejects_unwritable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.os.access",
        lambda *_: False,
    )

    with pytest.raises(PermissionError, match=r"无法创建 \.boxteam"):
        validate_workspace_root_access(workspace_root)


def test_validate_workspace_root_access_rejects_unwritable_metadata_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    metadata_root = workspace_root / ".boxteam"
    metadata_root.mkdir(parents=True)
    calls: list[Path] = []

    def fake_access(path: Path, _: int) -> bool:
        calls.append(path)
        return path != metadata_root

    monkeypatch.setattr("app.gateway.runtime.local_workspace.os.access", fake_access)

    with pytest.raises(PermissionError, match="保存会话和服务状态"):
        validate_workspace_root_access(workspace_root)
    assert calls == [workspace_root, metadata_root]
