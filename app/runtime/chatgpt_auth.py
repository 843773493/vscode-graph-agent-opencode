from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.path_utils import get_boxteam_home


CHATGPT_TOKEN_DIR_ENV = "CHATGPT_TOKEN_DIR"
CHATGPT_AUTH_FILE_ENV = "CHATGPT_AUTH_FILE"
DEFAULT_CHATGPT_AUTH_FILE = "auth.json"
CHATGPT_OAUTH_TYPE = "oauth"
CHATGPT_OAUTH_METHOD = "chatgpt"


def is_chatgpt_oauth_provider(provider: Mapping[str, object]) -> bool:
    """判断 provider 是否显式选择 ChatGPT OAuth。"""
    auth = provider.get("auth")
    return (
        isinstance(auth, Mapping)
        and auth.get("type") == CHATGPT_OAUTH_TYPE
        and auth.get("method") == CHATGPT_OAUTH_METHOD
    )


def configure_litellm_chatgpt_auth_directory() -> Path:
    """配置凭据目录，并在首次使用时从 Codex 原生凭据迁移。"""
    configured = os.environ.get(CHATGPT_TOKEN_DIR_ENV)
    if configured:
        token_dir = Path(configured).expanduser().resolve()
    else:
        token_dir = get_boxteam_home() / "auth" / "chatgpt"
        os.environ[CHATGPT_TOKEN_DIR_ENV] = str(token_dir)

    auth_file = _resolve_litellm_auth_file(token_dir)
    if auth_file.is_file():
        return token_dir

    codex_auth_file = Path("~/.codex/auth.json").expanduser().resolve()
    if codex_auth_file.is_file():
        _migrate_codex_auth(codex_auth_file, auth_file)
    return token_dir


def ensure_litellm_chatgpt_model_capabilities(model: str) -> bool:
    """在 LiteLLM 尚未识别 GPT-5.6 时临时登记 Responses 能力。"""
    normalized_model = model.removeprefix("chatgpt/")
    if not normalized_model.startswith("gpt-5.6-"):
        return False

    from litellm import register_model
    from litellm.utils import supports_native_streaming

    if supports_native_streaming(
        model=normalized_model,
        custom_llm_provider="chatgpt",
    ):
        return False

    # TODO: LiteLLM 原生模型目录识别 GPT-5.6 ChatGPT streaming 后删除此兼容注册。
    register_model(
        {
            f"chatgpt/{normalized_model}": {
                "litellm_provider": "chatgpt",
                "mode": "responses",
                "supported_endpoints": ["/v1/responses"],
                "supports_function_calling": True,
                "supports_parallel_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_vision": True,
                "supports_native_streaming": True,
            }
        }
    )
    return True


def _resolve_litellm_auth_file(token_dir: Path) -> Path:
    configured_name = os.environ.get(
        CHATGPT_AUTH_FILE_ENV,
        DEFAULT_CHATGPT_AUTH_FILE,
    )
    return (token_dir / configured_name).expanduser().resolve()


def _migrate_codex_auth(source: Path, target: Path) -> None:
    source_data = json.loads(source.read_text(encoding="utf-8"))
    tokens = source_data.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError(f"Codex 认证文件缺少 tokens 对象: {source}")

    required = ("access_token", "refresh_token", "id_token", "account_id")
    missing = [
        key
        for key in required
        if not isinstance(tokens.get(key), str) or not tokens[key]
    ]
    if missing:
        raise ValueError(
            f"Codex 认证文件缺少 LiteLLM 所需字段: {', '.join(missing)}"
        )

    record: dict[str, Any] = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "id_token": tokens["id_token"],
        "expires_at": _jwt_exp(tokens["access_token"]),
        "account_id": tokens["account_id"],
    }
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".auth.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            return
        os.chmod(target, 0o600)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Codex access token 不是有效的 JWT")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(encoded))
    expires_at = claims.get("exp")
    if not isinstance(expires_at, int):
        raise ValueError("Codex access token JWT 缺少整数 exp")
    return expires_at
