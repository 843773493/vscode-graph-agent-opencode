from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from traceback import format_exception
from typing import Literal

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
            raise _file_access_error(self._storage_path, "federation_credentials", "read", stage, error) from error

    def _save(self, credentials: dict[str, FederationCredential]) -> None:
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
        descriptor: int | None = None
        temporary_path: Path | None = None
        primary_error: BaseException | None = None
        stage = "prepare"
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            stage = "create_temp"
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._storage_path.name}.", dir=self._storage_path.parent,
            )
            temporary_path = Path(temporary_name)
            stage = "open_stream"
            file = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None  # fdopen成功后由同一stream负责关闭；失败仍由本次finally持有。
            with file:
                stage = "permissions"
                restrict_private_path_access(
                    temporary_path,
                    descriptor=file.fileno(),
                )
                stage = "write"
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                stage = "flush"
                file.flush()
                stage = "fsync"
                os.fsync(file.fileno())
                stage = "close"
            stage = "publish"
            os.replace(temporary_path, self._storage_path)
            temporary_path = None
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            primary_error = _file_access_error(self._storage_path, "federation_credentials", "write", stage, error)
            raise primary_error from error
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_errors: list[BaseException] = []
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as error:
                    cleanup_errors.append(_file_access_error(self._storage_path, "federation_credentials", "write", "close_descriptor", error))
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as error:
                    cleanup_errors.append(_file_access_error(self._storage_path, "federation_credentials", "write", "cleanup", error))
            if cleanup_errors:
                if primary_error is not None:
                    cleanup_errors.insert(0, primary_error)
                if len(cleanup_errors) == 1:
                    raise cleanup_errors[0]
                raise BaseExceptionGroup(
                    "联邦凭据发布与收尾均失败: artifact=federation_credentials, stage=cleanup", cleanup_errors,
                ) from primary_error


def _file_access_error(
    storage_path: Path,
    artifact: Literal["federation_credentials", "gateway_identity"],
    operation: Literal["read", "write"],
    stage: str,
    error: OSError | ValueError | TypeError | KeyError | RuntimeError,
) -> Exception:
    action = "读取" if operation == "read" else "写入"
    label = "Gateway 身份文件非法" if artifact == "gateway_identity" else f"联邦凭据{action}失败"
    detail = f"{label}: artifact={artifact}, stage={stage}, error={type(error).__name__}"
    if isinstance(error, OSError):
        detail += f", errno={error.errno}, winerror={getattr(error, 'winerror', None)}"
    # 解码与字段校验的原异常可能含凭据正文，只保留在cause，不写入日志。
    try:
        logger.error("%s path=%s request_id=%s", detail, storage_path, get_current_request_id(),
                     exc_info=(type(error), error, error.__traceback__) if isinstance(error, (OSError, RuntimeError)) else None)
    except Exception as sink_error:  # noqa: BLE001 — 次错留在原原因，不能改变认证拒绝或阻断原收尾。
        error.add_note("诊断日志输出失败：\n" + "".join(format_exception(sink_error)))
        detail += f", diagnostic_sink_error={type(sink_error).__name__}"
    projected: Exception
    if isinstance(error, OSError):
        if error.errno is None:
            projected = type(error)(detail)
        else:
            projected = type(error)(error.errno, detail, None, getattr(error, "winerror", None))
    elif isinstance(error, UnicodeDecodeError):
        projected = UnicodeDecodeError(error.encoding, error.object, error.start, error.end, detail)
    elif isinstance(error, UnicodeEncodeError):
        projected = UnicodeEncodeError(error.encoding, error.object, error.start, error.end, detail)
    elif isinstance(error, json.JSONDecodeError):
        projected = json.JSONDecodeError(detail, error.doc, error.pos)
    else:
        projected = type(error)(detail)
    projected.__cause__ = error
    return projected

def load_or_create_gateway_id(storage_path: Path) -> str:
    descriptor: int | None = None
    primary_error: BaseException | None = None
    stage = "stat"
    try:
        if storage_path.exists():
            stage = "read"
            with storage_path.open("r", encoding="utf-8") as file:
                stage = "decode"
                payload = json.load(file)
                stage = "close"
            stage = "validate"
            gateway_id = payload.get("gateway_id") if isinstance(payload, dict) else None
            if not isinstance(gateway_id, str) or not gateway_id:
                raise ValueError("Gateway 身份文件缺少有效 gateway_id")
            return validate_gateway_id(gateway_id)
        stage = "prepare"
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        gateway_id = f"gateway_{secrets.token_hex(16)}"
        stage = "create"
        descriptor = os.open(storage_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stage = "open_stream"
        file = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None  # stream 创建成功后才转交关闭责任，不删除失败身份或替换已有身份。
        with file:
            stage = "write"
            json.dump({"gateway_id": gateway_id}, file)
            file.write("\n")
            stage = "close"
        return gateway_id
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        primary_error = _file_access_error(storage_path, "gateway_identity", "read" if stage in {
            "stat", "read", "decode", "validate",
        } else "write", stage, error)
        raise primary_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_error = _file_access_error(storage_path, "gateway_identity", "write", "close_descriptor", error)
                if primary_error is not None:
                    raise BaseExceptionGroup(
                        "Gateway 身份创建与收尾均失败: artifact=gateway_identity, stage=close_descriptor",
                        [primary_error, cleanup_error],
                    ) from primary_error
                raise cleanup_error from error
