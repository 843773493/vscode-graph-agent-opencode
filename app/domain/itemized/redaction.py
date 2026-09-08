"""敏感 hash preimage 的纯值 API；session key 的生成、持久化归调用方。"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, payload_content_length

HashPath = tuple[str | int, ...]
_MARKER_FIELDS = frozenset(
    {"redaction_class", "content_length", "redacted_stable_digest"}
)


def validate_hash_redaction(value: Mapping[str, object]) -> dict[str, object]:
    """恢复 marker 不需要 key 或原文，但必须严格保留其三个完整性字段。"""
    if set(value) != _MARKER_FIELDS:
        raise ItemSchemaError(
            "hash-redaction-invalid: marker 必须恰有 class/length/digest"
        )
    redaction_class = value["redaction_class"]
    if (
        not isinstance(redaction_class, str)
        or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", redaction_class) is None
    ):
        raise ItemSchemaError(
            "hash-redaction-invalid: redaction_class 必须是非空分类标识"
        )
    length = value["content_length"]
    if type(length) is not int or length < 0:
        raise ItemSchemaError("hash-redaction-invalid: content_length 必须是非负整数")
    digest = value["redacted_stable_digest"]
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"hmac-sha256:session:v1:[0-9a-f]{64}", digest) is None
    ):
        raise ItemSchemaError(
            "hash-redaction-invalid: digest 必须是 session v1 HMAC token"
        )
    return dict(value)


@dataclass(frozen=True, slots=True)
class SessionHashRedactor:
    """调用方必须传入该 session 独占、可在重启后恢复的至少 256-bit key。

    本值对象不生成默认 key、不访问存储、不把 key 放进 repr 或 hash preimage。
    digest 与现有 protected detail 合同一致，HMAC 输入是 JCS(value)；class
    与 logical length 由外层 preimage 绑定。字符串长度是原文 UTF-8 字节数，
    其它 JSON 值是 JCS 字节数。不同 session 必须使用不同 key。
    """

    session_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.session_key, bytes) or len(self.session_key) < 32:
            raise ItemSchemaError(
                "hash-redaction-key-required: session key 至少需要 32 bytes"
            )

    def redact(self, value: object, *, redaction_class: str) -> dict[str, object]:
        encoded = canonical_json_bytes(value)
        return validate_hash_redaction(
            {
                "redaction_class": redaction_class,
                "content_length": (
                    payload_content_length("text", value)
                    if isinstance(value, str)
                    else len(encoded)
                ),
                "redacted_stable_digest": "hmac-sha256:session:v1:"
                + hmac.new(self.session_key, encoded, hashlib.sha256).hexdigest(),
            }
        )

    def redact_paths(
        self,
        value: object,
        *,
        classes: Mapping[HashPath, str],
    ) -> object:
        """按 producer 显式分类的 JSON 路径生成副本；路径缺失或重叠直接失败。

        不依据正文猜测敏感度，不改写 provider 原始请求；root 路径为 ()。
        任意私密 message/detail 必须由 owner 分类，字段名检测不能代替它。
        """
        for path in classes:
            if not isinstance(path, tuple) or any(
                not isinstance(part, str) and (type(part) is not int or part < 0)
                for part in path
            ):
                raise ItemSchemaError(
                    "hash-redaction-path-invalid: 必须使用字符串/非负整数 tuple"
                )
        visited: set[HashPath] = set()

        def visit(child: object, path: HashPath) -> object:
            if path in classes:
                visited.add(path)
                return self.redact(child, redaction_class=classes[path])
            if isinstance(child, Mapping):
                if not all(isinstance(key, str) for key in child):
                    raise ItemSchemaError("hash redaction object key 必须是字符串")
                return {key: visit(item, (*path, key)) for key, item in child.items()}
            if isinstance(child, (list, tuple)):
                return [visit(item, (*path, index)) for index, item in enumerate(child)]
            return child

        result = visit(value, ())
        if visited != set(classes):
            raise ItemSchemaError("hash-redaction-path-invalid: 路径缺失或重叠")
        canonical_json_bytes(result)
        return result


__all__ = ["HashPath", "SessionHashRedactor", "validate_hash_redaction"]
