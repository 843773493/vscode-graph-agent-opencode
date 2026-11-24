from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from app.core.failures import failure_detail, mark_provider_failure, mark_public_failure
from app.core.file_permissions import restrict_private_path_access
from app.core.path_utils import get_boxteam_home
from app.runtime.network_proxy import ProxyRoutes

CHATGPT_TOKEN_DIR_ENV = "CHATGPT_TOKEN_DIR"
CHATGPT_AUTH_FILE_ENV = "CHATGPT_AUTH_FILE"
DEFAULT_CHATGPT_AUTH_FILE = "auth.json"
CHATGPT_OAUTH_TYPE = "oauth"
CHATGPT_OAUTH_METHOD = "chatgpt"
TOKEN_EXPIRY_SKEW_SECONDS = 60
_auth_refresh_lock = Lock()


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
    configured_token_directory = os.environ.get(CHATGPT_TOKEN_DIR_ENV)
    if configured_token_directory:
        token_directory = Path(configured_token_directory).expanduser().resolve()
    else:
        token_directory = get_boxteam_home() / "auth" / "chatgpt"
        os.environ[CHATGPT_TOKEN_DIR_ENV] = str(token_directory)

    auth_file = _resolve_litellm_auth_file(token_directory)
    if auth_file.is_file():
        return token_directory

    codex_auth_file = Path("~/.codex/auth.json").expanduser().resolve()
    if codex_auth_file.is_file():
        _publish_auth_record(auth_file, _build_litellm_auth_record(codex_auth_file), replace_existing=False)
    return token_directory


@dataclass(frozen=True, slots=True)
class ChatGPTCredential:
    access_token: str = field(repr=False)
    account_id: str | None


def ensure_chatgpt_oauth_ready(token_directory: Path) -> str | None:
    """普通配置检查只使用现成凭据，不刷新或进入交互式登录。"""
    return _ready_chatgpt_credential(token_directory, routes=None).account_id


def prepare_chatgpt_oauth(token_directory: Path, *, routes: ProxyRoutes) -> ChatGPTCredential:
    """正式调用沿认证 Provider 的路由刷新凭据，绝不触发设备登录。"""
    # 同一凭据文件中的 refresh token 会轮换；先重读再刷新，避免并发消费旧 token。
    with _auth_refresh_lock:
        return _ready_chatgpt_credential(token_directory, routes=routes)


def _ready_chatgpt_credential(token_directory: Path, *, routes: ProxyRoutes | None) -> ChatGPTCredential:
    auth_file = _resolve_litellm_auth_file(token_directory)
    if not auth_file.is_file():
        raise RuntimeError(
            "ChatGPT OAuth 凭据不存在。请先执行 codex login，再重试请求。"
        )

    with _auth_file_access(auth_file, artifact="chatgpt_auth", stage="read"):
        auth_text = auth_file.read_text(encoding="utf-8")
    auth_data = json.loads(auth_text)
    access_token = auth_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        reason = "ChatGPT OAuth 凭据缺少 access_token"
        error = ValueError(reason)
        error.add_note(f"OAuth 凭据位置: {auth_file}")
        raise ValueError(failure_detail(
            error, stage="validate", label=f"{reason}: artifact=chatgpt_auth",
        )) from error
    expires_at = auth_data.get("expires_at")
    if not isinstance(expires_at, int):
        expires_at = _jwt_exp(access_token)
    if time.time() >= expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
        codex_auth_file = Path("~/.codex/auth.json").expanduser().resolve()
        source_record = _build_litellm_auth_record(codex_auth_file) if codex_auth_file.is_file() else None
        if source_record is not None and time.time() < source_record["expires_at"] - TOKEN_EXPIRY_SKEW_SECONDS:
            auth_data = source_record
        elif routes is not None and isinstance(auth_data.get("refresh_token"), str) and auth_data["refresh_token"]:
            auth_data = _refresh_chatgpt_tokens(auth_data, routes=routes)
        else:
            raise RuntimeError(
                "ChatGPT OAuth 凭据已过期，且未找到有效的 Codex 凭据。"
                "请先执行 codex login，再重试请求。"
            )
        _publish_auth_record(auth_file, auth_data, replace_existing=True)
    account_id = auth_data.get("account_id")
    return ChatGPTCredential(
        access_token=auth_data["access_token"],
        account_id=account_id if isinstance(account_id, str) and account_id else None,
    )


def _refresh_chatgpt_tokens(auth_data: Mapping[str, Any], *, routes: ProxyRoutes) -> dict[str, Any]:
    from app.runtime.model_stream import is_model_stream_replay

    if is_model_stream_replay():
        raise RuntimeError("模型回放禁止刷新已过期的 ChatGPT OAuth 凭据；请先在真实模式更新凭据")
    # 只复用 SDK 的认证协议常量；不借用其进程级 HTTP client 或 authenticator 缓存。
    from app.runtime.litellm_runtime import load_litellm

    load_litellm()
    from litellm.llms.chatgpt.common_utils import (
        CHATGPT_CLIENT_ID,
        CHATGPT_OAUTH_TOKEN_URL,
    )

    verify = httpx.create_ssl_context(trust_env=True)
    try:
        with httpx.Client(verify=verify, trust_env=False, mounts=routes.sync_mounts(verify=verify), timeout=30) as client:
            response = client.post(CHATGPT_OAUTH_TOKEN_URL, json={
                "client_id": CHATGPT_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": auth_data["refresh_token"],
                "scope": "openid profile email",
            })
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        proxy_secrets = tuple(part.decode() for _pattern, proxy in routes.routes if proxy is not None and proxy.raw_auth
                              for part in proxy.raw_auth)
        mark_provider_failure(error, secrets=(auth_data["refresh_token"], auth_data["access_token"], *proxy_secrets))
        raise
    required = ("access_token", "id_token")
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key] for key in required):
        raise ValueError("ChatGPT OAuth 刷新响应缺少 access_token 或 id_token")
    access_token = data["access_token"]
    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token") or auth_data["refresh_token"],
        "id_token": data["id_token"],
        "expires_at": _jwt_exp(access_token),
        "account_id": _jwt_account_id(data["id_token"]) or _jwt_account_id(access_token) or auth_data.get("account_id"),
    }


def ensure_litellm_chatgpt_model_capabilities(model: str) -> bool:
    """在 LiteLLM 尚未识别 GPT-5.6 时临时登记 Responses 能力。"""
    normalized_model = model.removeprefix("chatgpt/")
    if not normalized_model.startswith("gpt-5.6-"):
        return False

    from app.runtime.litellm_runtime import litellm

    if litellm.utils.supports_native_streaming(
        model=normalized_model,
        custom_llm_provider="chatgpt",
    ):
        return False

    # TODO: LiteLLM 原生模型目录识别 GPT-5.6 ChatGPT streaming 后删除此兼容注册。
    litellm.register_model(
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


def _resolve_litellm_auth_file(token_directory: Path) -> Path:
    configured_auth_filename = os.environ.get(
        CHATGPT_AUTH_FILE_ENV,
        DEFAULT_CHATGPT_AUTH_FILE,
    )
    return (token_directory / configured_auth_filename).expanduser().resolve()


@contextmanager
def _auth_file_access(source: Path, *, artifact: str, stage: str) -> Iterator[None]:
    """同一凭据链路保留原生错误事实，位置与完整原因仅留诊断。"""
    try:
        yield
    except (OSError, RuntimeError) as error:
        error.add_note(f"OAuth 凭据位置: {source}")
        detail = failure_detail(
            error, stage=stage, label=f"OAuth 凭据访问失败: artifact={artifact}",
        )
        if isinstance(error, OSError):
            raise mark_public_failure(type(error)(error.errno, detail, None, getattr(error, "winerror", None))) from error
        raise mark_public_failure(RuntimeError(detail)) from error


def _build_litellm_auth_record(source: Path) -> dict[str, Any]:
    with _auth_file_access(source, artifact="codex_auth", stage="read"):
        source_text = source.read_text(encoding="utf-8")
    source_data = json.loads(source_text)
    tokens = source_data.get("tokens")
    if not isinstance(tokens, dict):
        reason = "Codex 认证文件缺少 tokens 对象"
        error = TypeError(reason)
        error.add_note(f"OAuth 凭据位置: {source}")
        raise TypeError(failure_detail(
            error, stage="validate", label=f"{reason}: artifact=codex_auth",
        )) from error

    required = ("access_token", "refresh_token", "id_token")
    missing = [
        key
        for key in required
        if not isinstance(tokens.get(key), str) or not tokens[key]
    ]
    if missing:
        raise ValueError(
            f"Codex 认证文件缺少 LiteLLM 所需字段: {', '.join(missing)}"
        )

    record = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "id_token": tokens["id_token"],
        "expires_at": _jwt_exp(tokens["access_token"]),
    }
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = _jwt_account_id(tokens["id_token"])
        if account_id is None:
            account_id = _jwt_account_id(tokens["access_token"])
    if account_id is not None:
        record["account_id"] = account_id
    return record


def _publish_auth_record(target: Path, record: dict[str, Any], *, replace_existing: bool) -> None:
    with _auth_file_access(target, artifact="chatgpt_auth", stage="prepare"):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        restrict_private_path_access(target.parent, is_directory=True)
        if replace_existing and target.exists():
            restrict_private_path_access(target)
    with _auth_file_access(target, artifact="chatgpt_auth", stage="create_temp"):
        fd, temporary_name = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=target.parent)
    temporary: Path | None = Path(temporary_name)
    primary_error: BaseException | None = None
    try:
        with (
            _auth_file_access(temporary, artifact="chatgpt_auth", stage="write"),
            os.fdopen(fd, "w", encoding="utf-8") as stream,
        ):
            # 先交给stream持有fd，权限设置失败也必须关闭本次描述符。
            restrict_private_path_access(temporary, descriptor=fd)
            json.dump(record, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        with _auth_file_access(target, artifact="chatgpt_auth", stage="publish"):
            if replace_existing:
                os.replace(temporary, target)
                temporary = None
            else:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    return
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary is not None:
            try:
                with _auth_file_access(temporary, artifact="chatgpt_auth", stage="cleanup"):
                    temporary.unlink(missing_ok=True)
            except (OSError, RuntimeError) as cleanup_error:
                if primary_error is not None:
                    raise BaseExceptionGroup(
                        "OAuth 凭据发布与临时清理均失败: artifact=chatgpt_auth, stage=cleanup",
                        [primary_error, cleanup_error],
                    ) from primary_error
                raise


def _jwt_exp(token: str) -> int:
    claims = _jwt_claims(token)
    expires_at = claims.get("exp")
    if not isinstance(expires_at, int):
        raise TypeError("Codex access token JWT 缺少整数 exp")
    return expires_at


def _jwt_account_id(token: str) -> str | None:
    try:
        claims = _jwt_claims(token)
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return None
    account_id = auth_claims.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Codex token 不是有效的 JWT")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(encoded))
    if not isinstance(claims, dict):
        raise TypeError("Codex token JWT payload 必须是对象")
    return claims
