"""安装域身份只在完整首次发布时创建，不能由运行路由推导。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.backend_identity import load_or_create_backend_id, read_backend_id


@pytest.mark.parametrize("content", [
    b"{", b"[]", b'{"backend_id":"00000000-0000-4000-8000-000000000001"}',
    b'{"schema_version":2,"backend_id":"00000000-0000-4000-8000-000000000001"}',
    b'{"schema_version":true,"backend_id":"00000000-0000-4000-8000-000000000001"}',
    b'{"schema_version":1,"backend_id":"00000000000040008000000000000001"}',
    b'{"schema_version":1,"backend_id":123}',
])
def test_corrupt_identity_is_preserved_instead_of_reassigned(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "state/backend/identity.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_or_create_backend_id(tmp_path)
    assert path.read_bytes() == content


def test_read_does_not_initialize_missing_installation_domain(tmp_path: Path) -> None:
    home = tmp_path / "unregistered-home"
    with pytest.raises(FileNotFoundError):
        read_backend_id(home)
    assert not home.exists()
