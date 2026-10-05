"""正式 fixture 的 catalog 生命周期，不参与生产 owner 装配。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.path_utils import (
    SessionCatalogComponents,
    build_session_catalog_components,
    catalog_provider_scope,
)


class FixtureCatalogOwner:
    """每个测试单独持有显式 components，结束或模拟重启时统一释放。"""

    def __init__(self) -> None:
        self._components: dict[tuple[Path, str], SessionCatalogComponents] = {}

    def get_catalog_components(
        self, sessions_root: Path, workspace_id: str
    ) -> SessionCatalogComponents:
        key = (sessions_root, workspace_id)
        components = self._components.get(key)
        if components is None:
            components = build_session_catalog_components(sessions_root, workspace_id)
            self._components[key] = components
        return components.get_catalog_components(sessions_root, workspace_id)

    def release(self, sessions_root: Path | None = None) -> None:
        errors: list[Exception] = []
        keys = [
            key for key in self._components
            if sessions_root is None or key[0] == sessions_root.resolve()
        ]
        for key in keys:
            try:
                self._components.pop(key).close()
            except Exception as error:  # noqa: BLE001 - 全量释放后聚合失败。
                errors.append(error)
        if errors:
            raise ExceptionGroup("fixture catalog 关闭失败", errors)


@contextmanager
def fixture_catalog_scope() -> Iterator[FixtureCatalogOwner]:
    """供函数或模块级 fixture 装配使用，离开装配边界即关闭连接。"""
    owner = FixtureCatalogOwner()
    with catalog_provider_scope(owner):
        try:
            yield owner
        except BaseException as error:
            try:
                owner.release()
            except Exception as close_error:  # noqa: BLE001 - 保留装配和关闭两个失败。
                raise BaseExceptionGroup(
                    "fixture 装配失败且 catalog 关闭失败", [error, close_error]
                ) from None
            raise
        else:
            owner.release()
