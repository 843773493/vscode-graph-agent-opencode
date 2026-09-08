"""详情文件的唯一路径、非跟随读取、排他发布和删除边界。"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.runtime.detail_keys import (
    SessionPathResolver,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_relative_path,
    protected_detail_relative_path,
)


class DetailFiles:
    def __init__(self, resolver: SessionPathResolver) -> None:
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
        self, ref: DetailRef, *, protected: bool = False, create: bool = False
    ) -> Path | None:
        relative = (
            protected_detail_relative_path(ref)
            if protected
            else detail_relative_path(ref)
        )
        root = self._resolver.resolve_session_node_for_runtime(ref.session_id)
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

    def read(self, ref: DetailRef, *, protected: bool = False) -> bytes:
        target = self.path(ref, protected=protected)
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

    def publish(self, ref: DetailRef, raw: bytes, *, protected: bool = False) -> Path:
        target = self.path(ref, protected=protected, create=True)
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
            self.path(ref, protected=protected)
            os.link(temporary, target, follow_symlinks=False)
            published = True
            temporary.unlink()
            temporary_created = False
        except BaseException as error:
            # 发布后清理临时 link 失败也不能留下无 record 可追踪的正文。
            if published:
                self.path(ref, protected=protected)
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
        """先检查两个 locator，防止删除普通 manifest 后才发现 protected symlink。"""
        targets = tuple(
            self.path(ref, protected=protected) for protected in (False, True)
        )
        return tuple(
            target
            for target in targets
            if target is not None and self._info(target) is not None
        )


__all__ = ["DetailFiles"]
