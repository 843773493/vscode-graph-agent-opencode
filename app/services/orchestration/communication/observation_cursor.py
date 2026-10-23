"""冷观察游标的认证编码、结构校验与期限检查。"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import zlib
from dataclasses import asdict
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.session_control_communication_ledger.observations import (
    RemoteObservationError,
    canonical_json,
)
from app.core.session_control_store import SessionControlStore
from app.services.business.communication.addresses import GlobalThreadAddress


def _encode_observation_cursor(
    store: SessionControlStore,
    source: GlobalThreadAddress,
    value: dict[str, object],
    *,
    prefix: str,
) -> str:
    nonce = secrets.token_bytes(12)
    raw = zlib.compress(canonical_json(value).encode())
    encrypted = AESGCM(store.observation_cursor_key(create=True)).encrypt(
        nonce,
        raw,
        canonical_json([prefix, asdict(source)]).encode(),
    )
    return prefix + base64.urlsafe_b64encode(nonce + encrypted).decode()


def _decode_observation_cursor(
    store: SessionControlStore,
    source: GlobalThreadAddress,
    cursor: str,
    *,
    prefix: str,
    now: datetime,
    allow_expired: bool = False,
) -> dict[str, object]:
    try:
        if not cursor.startswith(prefix):
            raise ValueError("cursor 版本错误")
        encoded = base64.b64decode(
            cursor[len(prefix):].encode(),
            altchars=b"-_",
            validate=True,
        )
        associated_data = canonical_json([prefix, asdict(source)]).encode()
        raw = AESGCM(store.observation_cursor_key()).decrypt(
            encoded[:12],
            encoded[12:],
            associated_data,
        )
        value = json.loads(zlib.decompress(raw))
        fields = {"o", "t", "r", "u", "p", "b", "k", "n", "c", "e"}
        if prefix == "rc1.":
            fields.add("f")
        if not isinstance(value, dict) or set(value) != fields:
            raise TypeError("cursor 结构错误")
        if not all(isinstance(value[key], str) for key in ("o", "r", "p", "e")):
            raise TypeError("cursor identity 结构错误")
        if type(value["n"]) is not int or value["n"] < 1 or type(value["c"]) is not int or value["c"] < 0:
            raise TypeError("cursor ordinal/预算结构错误")
        if not isinstance(value["k"], str) or not isinstance(value["u"], list) or not isinstance(value["b"], list):
            raise TypeError("cursor offset/上界结构错误")
        GlobalThreadAddress(**value["t"])
        if prefix == "rc1." and not isinstance(value["f"], str):
            raise TypeError("cursor 原 page 结构错误")
        expires = datetime.fromisoformat(value["e"])
        if expires.tzinfo is None:
            raise ValueError("cursor 无时区")
    except RemoteObservationError:
        raise
    except (
        InvalidTag,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        zlib.error,
    ) as error:
        raise RemoteObservationError("read-snapshot-invalid") from error
    if not allow_expired and expires <= now:
        raise RemoteObservationError("read-snapshot-expired")
    return value
