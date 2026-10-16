"""详情文件的唯一路径、非跟随读取、排他发布和删除边界。"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
from contextlib import closing, nullcontext
from pathlib import Path

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCreationRecord
from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)


class DetailFiles:
    def __init__(self, resolver: SessionCatalogPathResolver) -> None:
        self._resolver = resolver

    @staticmethod
    def _info(path: Path) -> os.stat_result | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise DetailUnavailableError(f"detail 路径不能是符号链接: {path}")
        return info

    def path(
        self, ref: DetailRef, *, create: bool = False,
        creation_record: SessionCreationRecord | None = None,
        storage_owner: tuple[str, str] | None = None,
    ) -> Path | None:
        relative = detail_relative_path(ref)
        owner_session, owner_thread = storage_owner or (ref.session_id, ref.thread_id)
        if create or creation_record is not None:
            ref.require_owner(owner_session, owner_thread)
        if creation_record is not None and creation_record.session_id != ref.session_id:
            raise ValueError("source-mismatch: detail 与 prepared Session creation owner 不一致")
        root = (
            self._resolver.resolve_thread_node(owner_session, owner_thread)
            if creation_record is None else self._resolver.resolve_prepared_thread_node(
                creation_record, ref.thread_id,
            )
        )
        root_info = self._info(root)
        if root_info is None or not stat.S_ISDIR(root_info.st_mode):
            raise DetailUnavailableError("session node 不是安全普通目录")
        current = root
        for component in relative.parts[:-1]:
            current = current / component
            info = self._info(current)
            if info is None:
                if not create:
                    return None
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                info = self._info(current)
            if info is None or not stat.S_ISDIR(info.st_mode):
                raise DetailUnavailableError(
                    f"detail 父路径不是安全普通目录: {current}"
                )
        target = current / relative.name
        info = self._info(target)
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise DetailUnavailableError(f"detail target 不是普通文件: {target}")
        try:
            target.resolve(strict=False).relative_to(root.resolve(strict=True))
        except ValueError as error:
            raise DetailUnavailableError("detail 路径越出 session node") from error
        return target

    def read(
        self, ref: DetailRef, *, session_id: str, thread_id: str,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> bytes:
        """历史 ref 保留原值；只从已授权的当前物理节点读取冻结正文。"""
        owner = (session_id, thread_id)
        if snapshot is not None:
            if snapshot.closed:
                raise RuntimeError("rollout read snapshot 已关闭")
            if (snapshot.session_id, snapshot.thread_id) != owner:
                raise ValueError("read snapshot 与 detail 目标 owner 不一致")
        if (ref.session_id, ref.thread_id) != owner:
            with (
                nullcontext(snapshot.connection) if snapshot is not None else closing(
                    sqlite3.connect(
                        (self._resolver.resolve_thread_node(*owner) / "rollout" / "index.sqlite").as_uri() + "?mode=ro",
                        uri=True,
                    )
                )
            ) as connection:
                require_historical_scope(
                    connection, ref.session_id, ref.thread_id, current_owner=owner,
                )
        target = self.path(ref, storage_owner=owner)
        if target is None:
            raise DetailUnavailableError(f"assembly detail 正文缺失: {ref.detail_id}")
        descriptor = -1
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("detail target 不是普通文件")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                return stream.read()
        except OSError as error:
            raise DetailUnavailableError(
                f"assembly detail 无法读取: {ref.detail_id}"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def publish(
        self, ref: DetailRef, raw: bytes, *, creation_record: SessionCreationRecord | None = None,
    ) -> Path:
        target = self.path(ref, create=True, creation_record=creation_record)
        if target is None:
            raise DetailUnavailableError("detail 写入目录未创建")
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        temporary_created = False
        published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            temporary_created = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # link 是排他发布，不能用 replace 覆盖已经存在的 immutable detail。
            self.path(ref, creation_record=creation_record)
            os.link(temporary, target, follow_symlinks=False)
            published = True
            temporary.unlink()
            temporary_created = False
        except BaseException as error:
            # 发布后清理临时 link 失败也不能留下无 record 可追踪的正文。
            if published:
                self.path(ref, creation_record=creation_record)
                target.unlink()
            if isinstance(error, OSError):
                raise DetailUnavailableError(
                    f"assembly detail 排他写入失败: {ref.detail_id}"
                ) from error
            raise
        finally:
            if temporary_created:
                temporary.unlink(missing_ok=True)
        return target

    def remove_paths(self, ref: DetailRef) -> tuple[Path, ...]:
        """删除只使用唯一 typed locator，不扫描任何旁挂位置。"""
        target = self.path(ref)
        return (target,) if target is not None and self._info(target) is not None else ()


__all__ = ["DetailFiles"]
