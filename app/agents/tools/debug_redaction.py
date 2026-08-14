from __future__ import annotations

import re
from collections.abc import Mapping

REDACTION_PLACEHOLDER = "<redacted: possible secret>"
REDACTION_NOTICE = (
    "部分值因变量名或内容疑似凭据而被隐藏；请改用类型、长度或空值判断完成调试。"
)

_SENSITIVE_NAMES = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "accesstoken",
        "accountkey",
        "adminpassword",
        "anthropicapikey",
        "apikey",
        "apikeys",
        "apisecret",
        "apitoken",
        "authorization",
        "auth",
        "awssessiontoken",
        "awssecretaccesskey",
        "bearer",
        "bearertoken",
        "clientsecret",
        "connectionstring",
        "connstr",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "dbpass",
        "dbpasswd",
        "dbpassword",
        "githubtoken",
        "ghtoken",
        "idtoken",
        "jwt",
        "masterkey",
        "npmtoken",
        "oauthtoken",
        "openaiapikey",
        "otp",
        "pass",
        "passphrase",
        "passwd",
        "password",
        "passwords",
        "personalaccesstoken",
        "privatekey",
        "pwd",
        "refreshtoken",
        "rootpassword",
        "sastoken",
        "secret",
        "secretaccesskey",
        "secretkey",
        "secrets",
        "sessionid",
        "sessionkey",
        "sessiontoken",
        "signingkey",
        "slacktoken",
        "sshkey",
        "token",
        "tokens",
        "userpassword",
    }
)

_SECRET_VALUE_PATTERNS = (
    re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]*"
        r"(?:-----END[A-Z ]*PRIVATE KEY-----)?"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"),
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abopsr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bsk-(?:[A-Za-z0-9_-]+-)?[A-Za-z0-9]{16,}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(
        r"\b(?:AccountKey|SharedAccessSignature|Password|Pwd)\s*=\s*"
        r"[^;\s'\"]+",
        re.IGNORECASE,
    ),
)

_TRIVIAL_VALUES = frozenset(
    {
        "",
        "none",
        "null",
        "nil",
        "undefined",
        "nan",
        "true",
        "false",
        "0",
        "-1",
        "[]",
        "{}",
        "()",
        "empty",
        "<empty>",
    }
)


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s_-]", "", name.lower())


def _unwrap(value: str) -> str:
    text = value.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"`":
        text = text[1:-1].strip()
    return text


def is_sensitive_name(name: str | None) -> bool:
    return bool(name) and _normalize_name(name) in _SENSITIVE_NAMES


def expression_mentions_sensitive_name(expression: str) -> bool:
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", expression)
    return any(is_sensitive_name(identifier) for identifier in identifiers)


def looks_like_secret_value(value: str | None) -> bool:
    if not value:
        return False
    return any(pattern.search(value) is not None for pattern in _SECRET_VALUE_PATTERNS)


def redact_variable_value(name: str | None, value: object) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    if not text or _unwrap(text).lower() in _TRIVIAL_VALUES:
        return text, False
    if is_sensitive_name(name) or looks_like_secret_value(text):
        return REDACTION_PLACEHOLDER, True
    return text, False


def redact_expression_value(expression: str, value: object) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    if not text or _unwrap(text).lower() in _TRIVIAL_VALUES:
        return text, False
    if expression_mentions_sensitive_name(expression) or looks_like_secret_value(text):
        return REDACTION_PLACEHOLDER, True
    return text, False


def redact_free_text(value: str) -> tuple[str, bool]:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTION_PLACEHOLDER, redacted)
    return redacted, redacted != value


def contains_redaction(value: object) -> bool:
    if value == REDACTION_PLACEHOLDER:
        return True
    if isinstance(value, Mapping):
        return any(contains_redaction(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_redaction(item) for item in value)
    return False
