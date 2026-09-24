"""Gateway 联邦身份：稳定 ``gateway_id`` 与 Ed25519 签名密钥。

channel 握手用既有 federation token 认证对端，再把对端在 hello 帧中声明的
``gateway_id``/公钥绑定到该 token 已认证的 ``peer_gateway_id``；grant 完整性
与 audience/path 校验都以这把绑定后的公钥为准，不新增第二套身份来源。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_EPOCH_STALE,
    FEDERATION_GRANT_INVALID_SIGNATURE,
    FEDERATION_UNKNOWN_PEER,
    FederationError,
)

_SIGNING_KEY_NAME = "identity-signing-key.pem"


def signing_key_path(gateway_root: Path) -> Path:
    """联邦签名私钥固定位于 Gateway 控制面目录，不写入工作区业务数据。"""

    return gateway_root / _SIGNING_KEY_NAME


def load_or_create_signing_key(gateway_root: Path) -> Ed25519PrivateKey:
    """读取或原子创建 Ed25519 私钥，权限必须为 0600。"""

    path = signing_key_path(gateway_root)
    if path.exists():
        # TODO: Windows 使用 ACL 而不是 POSIX mode bits；临时文件 ACL 由系统继承控制。
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise PermissionError(f"Gateway 联邦签名私钥权限必须为 0600: {path}")
        private_key = serialization.load_pem_private_key(
            path.read_bytes(), password=None
        )
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError(f"Gateway 联邦签名私钥类型非法: {path}")
        return private_key
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    payload = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(payload)
    return private_key


def public_key_pem(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def load_public_key(pem: str) -> Ed25519PublicKey:
    try:
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception as error:  # PEM 解析失败统一为显式协议错误
        raise FederationError(
            FEDERATION_UNKNOWN_PEER, "对端公钥 PEM 无法解析", detail={"reason": str(error)}
        ) from error
    if not isinstance(public_key, Ed25519PublicKey):
        raise FederationError(FEDERATION_UNKNOWN_PEER, "对端公钥必须是 Ed25519")
    return public_key


@dataclass(frozen=True, slots=True)
class FederationPeerIdentity:
    """已认证对端的稳定身份：``gateway_id`` 与绑定公钥。"""

    gateway_id: str
    connection_id: str
    public_key_pem: str

    def verify(self, canonical_bytes: bytes, signature: bytes) -> None:
        try:
            load_public_key(self.public_key_pem).verify(signature, canonical_bytes)
        except FederationError:
            raise
        except Exception as error:  # 底层签名异常统一为协议错误
            raise FederationError(
                FEDERATION_GRANT_INVALID_SIGNATURE,
                f"grant 签名校验失败: issuer={self.gateway_id}",
            ) from error

    def assert_same_channel_epoch(self, other_epoch: int) -> None:
        """占位串口校验入口，避免调用方各自比较瞬时字段。"""

        if not isinstance(other_epoch, int) or other_epoch < 1:
            raise FederationError(
                FEDERATION_CHANNEL_EPOCH_STALE, "channel epoch 非法"
            )


def peer_identity_from_hello(
    *,
    expected_peer_gateway_id: str,
    connection_id: str,
    hello_gateway_id: object,
    hello_public_key_pem: object,
) -> FederationPeerIdentity:
    """把 hello 声明的身份绑定到 token 已认证的 ``peer_gateway_id``。"""

    if hello_gateway_id != expected_peer_gateway_id:
        raise FederationError(
            FEDERATION_UNKNOWN_PEER,
            "hello 声明的 gateway_id 与 token 认证的对端不一致",
            detail={
                "expected": expected_peer_gateway_id,
                "actual": hello_gateway_id,
            },
        )
    if not isinstance(hello_public_key_pem, str) or not hello_public_key_pem.strip():
        raise FederationError(FEDERATION_UNKNOWN_PEER, "hello 缺少对端公钥")
    load_public_key(hello_public_key_pem)
    return FederationPeerIdentity(
        gateway_id=expected_peer_gateway_id,
        connection_id=connection_id,
        public_key_pem=hello_public_key_pem,
    )


__all__ = [
    "FederationPeerIdentity",
    "load_or_create_signing_key",
    "load_public_key",
    "peer_identity_from_hello",
    "public_key_pem",
    "signing_key_path",
]
