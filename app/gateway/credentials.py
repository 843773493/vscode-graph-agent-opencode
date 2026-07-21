from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FederationCredential:
    connection_id: str
    peer_gateway_id: str
    token: str
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


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
            expires_at=datetime.now(timezone.utc) + lifetime,
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
        for credential in self._load().values():
            if not credential.expired and secrets.compare_digest(credential.token, token):
                return credential
        raise PermissionError("无效或已过期的 Gateway 联邦凭据")

    def list_valid(self) -> tuple[FederationCredential, ...]:
        return tuple(
            credential
            for credential in self._load().values()
            if not credential.expired
        )

    def remove(self, connection_id: str) -> None:
        credentials = self._load()
        if credentials.pop(connection_id, None) is not None:
            self._save(credentials)

    def _load(self) -> dict[str, FederationCredential]:
        if not self._storage_path.exists():
            return {}
        if self._storage_path.stat().st_mode & 0o077:
            raise PermissionError(
                f"联邦凭据文件权限必须为 0600: {self._storage_path}"
            )
        payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"联邦凭据文件版本非法: {self._storage_path}")
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
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self._storage_path)
        finally:
            temporary_path.unlink(missing_ok=True)


def load_or_create_gateway_id(storage_path: Path) -> str:
    if storage_path.exists():
        payload = json.loads(storage_path.read_text(encoding="utf-8"))
        gateway_id = payload.get("gateway_id") if isinstance(payload, dict) else None
        if not isinstance(gateway_id, str) or not gateway_id:
            raise ValueError(f"Gateway 身份文件非法: {storage_path}")
        return gateway_id
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
