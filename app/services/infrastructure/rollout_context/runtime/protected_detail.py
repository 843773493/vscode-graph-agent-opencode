"""Context detail 的可验证加密正文 backend。"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    protected_aad,
)


class ProtectedDetailError(RuntimeError):
    """受保护正文无法验证或解密。"""


class ProtectedDetailBackend:
    """使用注入密钥保存 AES-GCM authenticated detail body。

    密钥只由 runtime owner 注入，既不写入 SQLite，也不落入 session 节点。
    加密 blob 的明文 envelope 仍携带 source/hash/length，读取时由 detail
    store 再与 SQLite manifest 比较，避免“能解密”被误当成“属于本 assembly”。
    """

    _MAGIC = b"boxteam-context-detail-v2\x00"
    _NONCE_LENGTH = 12

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) not in {16, 24, 32}:
            raise ValueError("protected detail key 必须是 16、24 或 32 字节")
        self._cipher = AESGCM(key)

    def encrypt(
        self,
        *,
        record: DetailRecord,
        detail: object,
        detail_content_hash: str,
    ) -> bytes:
        plaintext = canonical_json_bytes(
            {
                **protected_aad(record),
                "detail_content_hash": detail_content_hash,
                "redacted_stable_digest": record.redacted_stable_digest,
                "detail": detail,
            }
        )
        nonce = secrets.token_bytes(self._NONCE_LENGTH)
        aad = canonical_json_bytes(protected_aad(record))
        return self._MAGIC + nonce + self._cipher.encrypt(nonce, plaintext, aad)

    def decrypt(
        self,
        blob: bytes,
        *,
        record: DetailRecord,
    ) -> Mapping[str, object]:
        return self._decrypt_authenticated(
            blob, magic=self._MAGIC, aad=canonical_json_bytes(protected_aad(record))
        )

    def authenticate_schema2_blob(
        self,
        blob: bytes,
        *,
        aad: bytes,
    ) -> Mapping[str, object]:
        """仅供显式 schema2→3 artifact 升级：使用调用方给定的原始 AAD。

        普通 decrypt 永远只接受 typed v2；此入口不访问旧路径、不生成旧格式。
        v1 header/AAD 定义已由历史源码读取记录冻结，见 protected_upgrade 模块。
        """
        if not isinstance(aad, bytes):
            raise ProtectedDetailError("schema-upgrade-aad 必须是 bytes")
        return self._decrypt_authenticated(
            blob,
            magic=b"boxteam-context-detail-v1\x00",
            aad=aad,
        )

    def _decrypt_authenticated(
        self,
        blob: bytes,
        *,
        magic: bytes,
        aad: bytes,
    ) -> Mapping[str, object]:
        if (
            not isinstance(blob, bytes)
            or not blob.startswith(magic)
            or len(blob) <= len(magic) + self._NONCE_LENGTH + 16
        ):
            raise ProtectedDetailError("protected detail blob 格式非法")
        nonce_start = len(magic)
        nonce_end = nonce_start + self._NONCE_LENGTH
        nonce = blob[nonce_start:nonce_end]
        ciphertext = blob[nonce_end:]
        failure = None
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, aad)
            value = json.loads(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError):
            failure = "protected detail authentication 失败"
        if failure is not None:
            # 在 except 之外抛出，异常链不能携带 decoder 保存的敏感原文。
            raise ProtectedDetailError(failure)
        if not isinstance(value, Mapping):
            raise ProtectedDetailError("protected detail plaintext 必须是 object")
        try:
            canonical_plaintext = canonical_json_bytes(value)
        except (TypeError, ValueError):
            failure = "protected detail plaintext 不是合法 canonical JSON"
        if failure is not None:
            raise ProtectedDetailError(failure)
        if canonical_plaintext != plaintext:
            raise ProtectedDetailError("protected detail plaintext 不是 canonical JSON")
        return value


__all__ = ["ProtectedDetailBackend", "ProtectedDetailError"]
