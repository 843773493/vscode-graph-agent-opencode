from pathlib import Path

import pytest

from app.gateway.runtime.local_workspace import validate_workspace_root_access


def test_validate_workspace_root_access_rejects_unwritable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.os.access",
        lambda *_: False,
    )

    with pytest.raises(PermissionError, match=r"无法创建 \.boxteam") as caught:
        validate_workspace_root_access(workspace_root)
    assert str(workspace_root) not in str(caught.value)
    assert str(workspace_root) in caplog.text
    assert not (workspace_root / ".boxteam").exists()


def test_validate_workspace_root_access_rejects_unwritable_metadata_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_root = tmp_path / "workspace"
    metadata_root = workspace_root / ".boxteam"
    metadata_root.mkdir(parents=True)
    calls: list[Path] = []

    def fake_access(path: Path, _: int) -> bool:
        calls.append(path)
        return path != metadata_root

    monkeypatch.setattr("app.gateway.runtime.local_workspace.os.access", fake_access)

    with pytest.raises(PermissionError, match="保存会话和服务状态") as caught:
        validate_workspace_root_access(workspace_root)
    assert calls == [workspace_root, metadata_root]
    assert str(workspace_root) not in str(caught.value)
    assert str(metadata_root) in caplog.text
    assert list(metadata_root.iterdir()) == []


@pytest.mark.parametrize("invalid_metadata", [False, True], ids=["missing-root", "metadata-file"])
def test_invalid_workspace_directory_keeps_native_location_in_log_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, invalid_metadata: bool,
) -> None:
    workspace_root = tmp_path / "native-location"
    if invalid_metadata:
        workspace_root.mkdir()
        (workspace_root / ".boxteam").write_bytes(b"preserve this file")
    error_type = NotADirectoryError if invalid_metadata else FileNotFoundError
    with pytest.raises(error_type) as caught:
        validate_workspace_root_access(workspace_root)
    assert str(workspace_root) not in str(caught.value)
    assert str(workspace_root) in caplog.text
    if invalid_metadata:
        assert (workspace_root / ".boxteam").read_bytes() == b"preserve this file"
    else:
        assert not workspace_root.exists()
