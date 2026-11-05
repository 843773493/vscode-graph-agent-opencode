"""真实独立进程共同首次创建目标安装域身份。"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from uuid import UUID

from app.core.backend_identity import load_or_create_backend_id, read_backend_id


def test_concurrent_first_creation_and_later_read_keep_one_identity(tmp_path: Path) -> None:
    home = tmp_path / "target-home"
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as executor:
        identities = list(executor.map(load_or_create_backend_id, [home] * 24))
    backend_id = identities[0]
    assert str(UUID(backend_id)) == backend_id
    assert set(identities) == {backend_id}
    assert read_backend_id(home) == backend_id
    assert load_or_create_backend_id(home) == backend_id
    other_home = tmp_path / "other-target-home"
    assert load_or_create_backend_id(other_home) != backend_id
