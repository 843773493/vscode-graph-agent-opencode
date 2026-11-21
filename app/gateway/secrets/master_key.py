"""主密钥只保存在目标侧受保护目录，不根据已有密文重新生成。"""

import os
from pathlib import Path

from app.core.atomic_fs import fsync_directory
from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)


def master_key(path: Path, *, create: bool) -> bytes:
    if not path.exists():
        if not create:
            raise RuntimeError("命名密钥库的主密钥缺失，请恢复原主密钥；不能重新生成")
        from app.runtime.secret_cipher import Fernet

        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            restrict_private_path_access(path, descriptor=stream.fileno())
            stream.write(Fernet.generate_key())
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(path.parent)
    with path.open("rb") as stream:
        require_private_file_access(path, descriptor=stream.fileno())
        return stream.read()
