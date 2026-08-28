from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .assets import Interaction, ModelStreamCassette
from .errors import ModelStreamMatchError

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "proxy_authorization",
    "set-cookie",
    "x-api-key",
}


@dataclass(frozen=True, slots=True)
class RequestSummary:
    method: str
    url: str
    safe_url: str
    request_id: str | None
    body_keys: tuple[str, ...]
    selected_body: dict[str, object]


def _safe_value(value: object, *, key: str | None = None) -> object:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key).casefold() not in {"messages", "prompt", "input"}
        }
    if isinstance(value, list):
        return ["[REDACTED_LIST]"]
    return str(value)


def request_summary(request: httpx.Request) -> RequestSummary:
    body: dict[str, object] = {}
    if request.content:
        try:
            parsed: object = json.loads(request.content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
    selected_body = _safe_value(body)
    if not isinstance(selected_body, dict):
        selected_body = {}
    input_types = _safe_input_types(body.get("input"))
    if input_types is not None:
        selected_body["input_types"] = input_types
    message_roles = _safe_message_roles(body.get("messages"))
    if message_roles is not None:
        selected_body["message_roles"] = message_roles
    return RequestSummary(
        method=request.method.upper(),
        url=str(request.url),
        safe_url=_safe_url(str(request.url)),
        request_id=request.headers.get("x-request-id"),
        body_keys=tuple(sorted(body.keys())),
        selected_body=selected_body,
    )


def _safe_input_types(value: object) -> list[str] | None:
    """只提取 Responses input 的结构类型，不把输入正文纳入匹配键。"""

    if not isinstance(value, list):
        return None
    types: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            types.append("<unknown>")
            continue
        item_type = item.get("type")
        types.append(item_type if isinstance(item_type, str) else "<unknown>")
    return types


def _safe_message_roles(value: object) -> list[str] | None:
    """只提取 Chat Completions 消息的角色序列，不保存消息正文。"""

    if not isinstance(value, list):
        return None
    roles: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            roles.append("<unknown>")
            continue
        role = item.get("role")
        roles.append(role if isinstance(role, str) else "<unknown>")
    return roles


def safe_request_match_fields(request: httpx.Request) -> dict[str, object]:
    """返回 recorder 可保存的 request match 字段。"""

    selected_body = request_summary(request).selected_body
    return {
        key: selected_body[key]
        for key in ("model", "stream", "input_types", "message_roles")
        if key in selected_body
    }


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(
        [(key, "[REDACTED]") for key, _value in parse_qsl(parts.query, keep_blank_values=True)]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def redact_url_for_asset(url: str) -> str:
    """只脱敏 URL 中的认证类 query 参数，保留其它匹配信息。"""

    parts = urlsplit(url)
    query = urlencode(
        [
            (
                key,
                "[REDACTED]" if key.casefold() in _SENSITIVE_KEYS else value,
            )
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _matches_subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _matches_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return expected == actual
    return expected == actual


class StrictRequestMatcher:
    """使用 method、完整 URL 和 request.match 做唯一匹配。"""

    def matching_interactions(
        self,
        cassette: ModelStreamCassette,
        request: httpx.Request,
    ) -> tuple[Interaction, ...]:
        summary = request_summary(request)
        matched = tuple(
            interaction
            for interaction in cassette.interactions
            if self._matches_interaction(interaction, summary)
        )
        return matched

    def require_one(
        self,
        cassette: ModelStreamCassette,
        request: httpx.Request,
        *,
        scenario_id: str,
    ) -> Interaction:
        summary = request_summary(request)
        matched = self.matching_interactions(cassette, request)
        if len(matched) == 1:
            return matched[0]
        if not matched:
            raise ModelStreamMatchError(
                self._error_message(
                    scenario_id=scenario_id,
                    cassette=cassette,
                    summary=summary,
                    reason="没有匹配 interaction",
                )
            )
        raise ModelStreamMatchError(
            self._error_message(
                scenario_id=scenario_id,
                cassette=cassette,
                summary=summary,
                reason=f"匹配到多个 interaction: {[item.index for item in matched]!r}",
            )
        )

    @staticmethod
    def _matches_interaction(
        interaction: Interaction,
        summary: RequestSummary,
    ) -> bool:
        expected = interaction.request
        return (
            expected.method == summary.method
            and redact_url_for_asset(expected.url) == redact_url_for_asset(summary.url)
            and _matches_subset(expected.match, summary.selected_body)
        )

    @staticmethod
    def _error_message(
        *,
        scenario_id: str,
        cassette: ModelStreamCassette,
        summary: RequestSummary,
        reason: str,
    ) -> str:
        path = str(cassette.path or "<memory>")
        candidates = [
            {
                "index": interaction.index,
                "method": interaction.request.method,
                "url": _safe_url(interaction.request.url),
                "match_keys": sorted(interaction.request.match.keys()),
            }
            for interaction in cassette.interactions
        ]
        return (
            "模型 stream strict matcher 失败: "
            f"scenario={scenario_id!r}, asset={path!r}, reason={reason}; "
            f"actual={{method: {summary.method!r}, url: {summary.safe_url!r}, "
            f"request_id: {summary.request_id!r}, body_keys: {summary.body_keys!r}, "
            f"body: {summary.selected_body!r}}}; candidates={candidates!r}"
        )
