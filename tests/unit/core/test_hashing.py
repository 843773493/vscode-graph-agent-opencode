"""app/core/hashing.py 裸 sha256 摘要原语测试。

守护真正重要的契约：``sha256_hex`` 返回裸 SHA-256 十六进制摘要（64 位小写、
无 ``sha256:`` 前缀），而不是换个算法、换成 base64 或漏编码。这些用例可杀
变异（例如把 sha256 换成 md5、把 ``hexdigest`` 换成 ``digest``、或对文本二次
编码）。只使用固定向量，不触碰真实工作区。
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from app.core.hashing import sha256_hex


def test_sha256_hex_matches_known_vector() -> None:
    """固定向量：空串与 ``b"abc"`` 的裸 sha256 摘要。"""
    assert sha256_hex(b"") == hashlib.sha256(b"").hexdigest()
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_hex_is_bare_lowercase_hex() -> None:
    """形状契约：64 位小写十六进制、无前缀、与十六进制解码后的字节一致。"""
    digest = sha256_hex(b"payload")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")
    assert not digest.startswith("sha256:")
    assert bytes.fromhex(digest) == hashlib.sha256(b"payload").digest()


def test_sha256_hex_distinguishes_byte_encoding() -> None:
    """编码契约：按字节哈希，同形文本的默认 utf-8 与 latin-1 字节给出不同摘要。"""
    utf8_bytes = "é".encode()
    latin1_bytes = "é".encode("latin-1")
    assert utf8_bytes != latin1_bytes
    assert sha256_hex(utf8_bytes) != sha256_hex(latin1_bytes)


# 语义不同的裸摘要实现，允许各自保留：签名与输入域都与 sha256_hex 不同。
_ALLOWED_OTHER_BARE_SHA256 = {
    ("app/core/thread_creation.py", "compute_artifact_manifest_hash"),
    ("app/services/infrastructure/attachment_blob_catalog/store.py", "_sha256_hex"),
    (
        "app/services/infrastructure/rollout_context/storage/resource_activation_migration.py",
        "_checksum",
    ),
    (
        "app/services/infrastructure/rollout_context/storage/resource_activation_store.py",
        "_checksum",
    ),
}


def test_bare_sha256_hexdigest_helper_has_single_neutral_definition() -> None:
    """裸 ``sha256(...).hexdigest()`` 助手只在 ``app/core/hashing.py`` 定义一次。

    该形态曾被复制到 rollout v2 持久化、Node Debug source capture 与 workspace
    内容修订等 16 处；任一处重新长出一份本地副本（哪怕逐字等价）都会让本断言
    失败，从而阻断摘要口径再次分叉。
    """
    app_root = Path.cwd() / "app"
    found: set[tuple[str, str]] = set()
    for path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [
                statement
                for statement in node.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                )
            ]
            if len(body) != 1 or not isinstance(body[0], ast.Return):
                continue
            if body[0].value is None:
                continue
            expression = ast.unparse(body[0].value)
            if (
                "hashlib.sha256" in expression
                and expression.endswith(".hexdigest()")
                and ":" not in expression
                and "+" not in expression
            ):
                found.add((str(path.relative_to(Path.cwd())), node.name))

    assert ("app/core/hashing.py", "sha256_hex") in found
    assert found - _ALLOWED_OTHER_BARE_SHA256 == {("app/core/hashing.py", "sha256_hex")}
