from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.path_utils import get_boxteam_home

CHATGPT_TOKEN_DIR_ENV = "CHATGPT_TOKEN_DIR"
CHATGPT_AUTH_FILE_ENV = "CHATGPT_AUTH_FILE"
DEFAULT_CHATGPT_AUTH_FILE = "auth.json"
CHATGPT_OAUTH_TYPE = "oauth"
CHATGPT_OAUTH_METHOD = "chatgpt"
TOKEN_EXPIRY_SKEW_SECONDS = 60


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


def ensure_chatgpt_oauth_ready(token_dir: Path) -> None:
    """确保服务请求只使用现成凭据，绝不进入交互式 device-code 登录。"""
    auth_file = _resolve_litellm_auth_file(token_dir)
    if not auth_file.is_file():
        raise RuntimeError(
            "ChatGPT OAuth 凭据不存在。请先执行 codex login，再重试请求。"
        )

    auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
    access_token = auth_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError(f"ChatGPT OAuth 凭据缺少 access_token: {auth_file}")
    expires_at = auth_data.get("expires_at")
    if not isinstance(expires_at, int):
        expires_at = _jwt_exp(access_token)
    if time.time() < expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
        return

    codex_auth_file = Path("~/.codex/auth.json").expanduser().resolve()
    if not codex_auth_file.is_file():
        raise RuntimeError(
            "ChatGPT OAuth 凭据已过期，且未找到可同步的 Codex 凭据。"
            "请先执行 codex login，再重试请求。"
        )
    source_record = _build_litellm_auth_record(codex_auth_file)
    source_expires_at = source_record["expires_at"]
    if time.time() >= source_expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
        raise RuntimeError(
            "ChatGPT OAuth 与 Codex 凭据均已过期。请先执行 codex login，再重试请求。"
        )
    _replace_auth_record(auth_file, source_record)


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
    record = _build_litellm_auth_record(source)
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


def _build_litellm_auth_record(source: Path) -> dict[str, Any]:
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

    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "id_token": tokens["id_token"],
        "expires_at": _jwt_exp(tokens["access_token"]),
        "account_id": tokens["account_id"],
    }


def _replace_auth_record(target: Path, record: dict[str, Any]) -> None:
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
        os.replace(temporary_name, target)
        temporary_name = ""
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
