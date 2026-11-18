"""单 Gateway writer 的小型密钥库；修改后原子发布，不缓存明文。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field

from app.core.atomic_fs import atomic_write_bytes
from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)
from app.gateway.secrets.master_key import master_key
from app.schemas.gateway.secrets import ResolvedSecret, SecretMetadataDTO


class SecretConflictError(ValueError):
    """原修订号已经过期，不能覆盖另一窗口的修改。"""


class _Record(SecretMetadataDTO):
    ciphertext: str = Field(repr=False)


class _Vault(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: int = Field(default=1, ge=1, le=1)
    items: list[_Record] = Field(default_factory=list)


class SecretVault:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = RLock()

    def _read(self) -> _Vault:
        path = self.directory / "vault.json"
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            return _Vault()
        with stream:
            require_private_file_access(path, descriptor=stream.fileno())
            vault = _Vault.model_validate_json(stream.read())
        if len({item.id for item in vault.items}) != len(vault.items):
            raise ValueError("命名密钥库包含重复 ID")
        return vault

    def _cipher(self):
        from app.runtime.secret_cipher import Fernet

        self.directory.mkdir(parents=True, exist_ok=True)
        restrict_private_path_access(self.directory, is_directory=True)
        key = master_key(self.directory / "master.key", create=not (self.directory / "vault.json").exists())
        return Fernet(key)

    def _write(self, vault: _Vault) -> None:
        atomic_write_bytes(self.directory / "vault.json", vault.model_dump_json(indent=2).encode())
        restrict_private_path_access(self.directory / "vault.json")

    @staticmethod
    def _metadata(vault: _Vault) -> list[SecretMetadataDTO]:
        return [SecretMetadataDTO.model_validate(item.model_dump(exclude={"ciphertext"})) for item in vault.items]

    def list(self) -> list[SecretMetadataDTO]:
        with self._lock:
            return self._metadata(self._read())

    def create(self, *, secret_id: str, name: str, value: str) -> SecretMetadataDTO:
        with self._lock:
            vault = self._read()
            if any(item.id == secret_id for item in vault.items):
                raise SecretConflictError("命名密钥 ID 已存在；请读取原目标元数据，不得重发创建")
            now = datetime.now(UTC).isoformat()
            item = _Record(id=secret_id, name=name.strip(), revision=1, created_at=now, updated_at=now,
                           ciphertext=self._cipher().encrypt(value.encode()).decode())
            self._write(_Vault(items=[*vault.items, item]))
            return SecretMetadataDTO.model_validate(item.model_dump(exclude={"ciphertext"}))

    def update(self, secret_id: str, *, expected_revision: int, name: str, value: str | None) -> list[SecretMetadataDTO]:
        with self._lock:
            vault = self._read()
            item = self._find(vault, secret_id, expected_revision)
            updated = item.model_copy(update={"name": name.strip(), "revision": item.revision + 1,
                "updated_at": datetime.now(UTC).isoformat(),
                "ciphertext": item.ciphertext if value is None else self._cipher().encrypt(value.encode()).decode()})
            replacement = _Vault(items=[updated if entry.id == secret_id else entry for entry in vault.items])
            self._write(replacement)
            return self._metadata(replacement)

    def delete(self, secret_id: str, *, expected_revision: int) -> list[SecretMetadataDTO]:
        with self._lock:
            vault = self._read()
            self._find(vault, secret_id, expected_revision)
            replacement = _Vault(items=[entry for entry in vault.items if entry.id != secret_id])
            self._write(replacement)
            return self._metadata(replacement)

    @staticmethod
    def _find(vault: _Vault, secret_id: str, revision: int | None = None) -> _Record:
        item = next((item for item in vault.items if item.id == secret_id), None)
        if item is None:
            raise KeyError(secret_id)
        if revision is not None and item.revision != revision:
            raise SecretConflictError("命名密钥已被其它操作修改，请重新读取后再操作")
        return item

    def resolve(self, ids: list[str]) -> dict[str, ResolvedSecret]:
        with self._lock:
            vault = self._read()
            selected = [self._find(vault, secret_id) for secret_id in dict.fromkeys(ids)]
            cipher = self._cipher()
            return {item.id: ResolvedSecret(revision=item.revision, value=cipher.decrypt(item.ciphertext.encode()).decode())
                    for item in selected}
