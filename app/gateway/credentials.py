from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)
from app.core.identifier import validate_gateway_id
from app.core.trace_middleware import get_current_request_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FederationCredential:
    connection_id: str
    peer_gateway_id: str
    token: str
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


class FederationCredentialStore:
    """独立保存 Gateway 联邦凭据，不把密钥混入工作区注册表。"""

    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path

    def issue(
        self,
        *,
        connection_id: str,
        peer_gateway_id: str,
        lifetime: timedelta = timedelta(days=30),
    ) -> FederationCredential:
        credential = FederationCredential(
            connection_id=connection_id,
            peer_gateway_id=peer_gateway_id,
            token=secrets.token_urlsafe(48),
            expires_at=datetime.now(UTC) + lifetime,
        )
        credentials = self._load()
        credentials[connection_id] = credential
        self._save(credentials)
        return credential

    def put(self, credential: FederationCredential) -> None:
        credentials = self._load()
        credentials[credential.connection_id] = credential
        self._save(credentials)

    def get(self, connection_id: str) -> FederationCredential:
        credential = self._load().get(connection_id)
        if credential is None:
            raise LookupError(f"远程 Gateway 连接缺少联邦凭据: {connection_id}")
        if credential.expired:
            raise PermissionError(f"远程 Gateway 联邦凭据已过期: {connection_id}")
        return credential

    def verify(self, token: str) -> FederationCredential:
        now = datetime.now(UTC)
        for credential in self._load().values():
            if (
                now < credential.expires_at
                and secrets.compare_digest(credential.token, token)
            ):
                return credential
        raise PermissionError("无效或已过期的 Gateway 联邦凭据")

    def list_valid(self) -> tuple[FederationCredential, ...]:
        now = datetime.now(UTC)
        return tuple(
            credential
            for credential in self._load().values()
            if now < credential.expires_at
        )

    def list_all(self) -> tuple[FederationCredential, ...]:
        """返回全部凭据，供控制面明确展示已授权和已过期连接。"""
        return tuple(self._load().values())

    def remove(self, connection_id: str) -> None:
        credentials = self._load()
        if credentials.pop(connection_id, None) is not None:
            self._save(credentials)

    def _load(self) -> dict[str, FederationCredential]:
        stage = "open"
        try:
            with self._storage_path.open("r", encoding="utf-8") as file:
                # 校验与解析使用同一文件句柄，避免重复路径查询和替换竞态。
                stage = "permissions"
                require_private_file_access(
                    self._storage_path,
                    descriptor=file.fileno(),
                )
                stage = "decode"
                payload = json.load(file)
                stage = "close"
            stage = "validate"
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError("联邦凭据文件版本非法")
            result: dict[str, FederationCredential] = {}
            for item in payload.get("credentials", []):
                credential = FederationCredential(
                    connection_id=str(item["connection_id"]),
                    peer_gateway_id=str(item["peer_gateway_id"]),
                    token=str(item["token"]),
                    expires_at=datetime.fromisoformat(str(item["expires_at"])),
                )
                result[credential.connection_id] = credential
            return result
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            # 只有尚未打开的缺文件表示空库；权限核验等后续失败不能被当作未配置。
            if stage == "open" and isinstance(error, FileNotFoundError):
                return {}
            raise self._read_error(stage, error) from error

    def _read_error(self, stage: str, error: OSError | ValueError | TypeError | KeyError | RuntimeError) -> Exception:
        detail = f"联邦凭据读取失败: artifact=federation_credentials, stage={stage}, error={type(error).__name__}"
        if isinstance(error, OSError):
            detail += f", errno={error.errno}, winerror={getattr(error, 'winerror', None)}"
        # 解码与字段校验的原异常可能含凭据正文，只保留在cause，不写入日志。
        logger.error("%s path=%s request_id=%s", detail, self._storage_path, get_current_request_id(),
                     exc_info=(type(error), error, error.__traceback__) if stage in {"open", "permissions", "close"} else None)
        if isinstance(error, OSError):
            if error.errno is None:
                return type(error)(detail)
            return type(error)(error.errno, detail, None, getattr(error, "winerror", None))
        if isinstance(error, UnicodeDecodeError):
            return UnicodeDecodeError(error.encoding, error.object, error.start, error.end, detail)
        if isinstance(error, json.JSONDecodeError):
            return json.JSONDecodeError(detail, error.doc, error.pos)
        return type(error)(detail)

    def _save(self, credentials: dict[str, FederationCredential]) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "credentials": [
                {
                    "connection_id": item.connection_id,
                    "peer_gateway_id": item.peer_gateway_id,
                    "token": item.token,
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in credentials.values()
            ],
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._storage_path.name}.",
            dir=self._storage_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                restrict_private_path_access(
                    temporary_path,
                    descriptor=file.fileno(),
                )
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self._storage_path)
        finally:
            temporary_path.unlink(missing_ok=True)


def load_or_create_gateway_id(storage_path: Path) -> str:
    if storage_path.exists():
        payload = json.loads(storage_path.read_text(encoding="utf-8"))
        gateway_id = payload.get("gateway_id") if isinstance(payload, dict) else None
        if not isinstance(gateway_id, str) or not gateway_id:
            raise ValueError(f"Gateway 身份文件非法: {storage_path}")
        try:
            return validate_gateway_id(gateway_id)
        except ValueError as error:
            raise ValueError(f"Gateway 身份文件非法: {storage_path}: {error}") from error
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    gateway_id = f"gateway_{secrets.token_hex(16)}"
    descriptor = os.open(
        storage_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump({"gateway_id": gateway_id}, file)
        file.write("\n")
    return gateway_id
