import re
from typing import NoReturn

from app.core.failures import failure_detail
from app.gateway.theme.builtins import WARM_TOKENS

CSS_FORBIDDEN = re.compile(r"[;{}]|url\s*\(", re.IGNORECASE)
_COLOR_VALUE = re.compile(
    r"^(?:#[0-9a-f]{3,8}|(?:rgb|rgba|hsl|hsla|lab|lch|oklab|oklch|color|color-mix|var)\(.+\)|transparent|currentColor|[a-z]+)$",
    re.IGNORECASE,
)
_LENGTH_VALUE = re.compile(r"^(?:0|\d+(?:\.\d+)?(?:px|rem|em|%)|var\(.+\))$")
_POSITION_SIZE_VALUE = re.compile(r"^[a-z0-9.%()\-+ /,]+$", re.IGNORECASE)
_COLOR_TOKENS = {
    name
    for name in WARM_TOKENS
    if any(
        marker in name
        for marker in (
            "background",
            "foreground",
            "border",
            "accent",
            "status",
            "text-",
            "icon-",
            "link-",
            "highlight",
        )
    )
} | {
    "--bt-chrome-surface",
    "--bt-workspace-surface",
    "--bt-panel-surface",
    "--bt-floating-surface",
    "--bt-critical-surface",
}
_COLOR_TOKENS -= {
    "--bt-background-image",
    "--bt-background-overlay",
    "--bt-background-position",
    "--bt-background-size",
    "--bt-background-repeat",
    "--bt-surface-border",
}
_LENGTH_TOKENS = {
    "--bt-font-size",
    "--bt-font-size-heading",
    "--bt-font-size-label",
    "--bt-radius-small",
    "--bt-radius-medium",
    "--bt-radius-large",
    "--bt-panel-radius",
    "--bt-surface-gap",
}


def reject_theme_value(*, rule: str, field: str, value: object) -> NoReturn:
    """字段和规则作为公开事实，未经校验的原值只进入诊断。"""
    error = ValueError(f"{rule}: field={field}, value={value!r}")
    raise ValueError(failure_detail(
        error, stage="theme-validate", label=f"{rule}: field={field}",
    )) from error


def validate_token(name: str, value: str) -> None:
    if name not in WARM_TOKENS:
        reject_theme_value(rule="未知主题 token", field="tokens", value={name: value})
    field = f"tokens.{name}"
    if not value.strip():
        reject_theme_value(rule="主题 token 不允许为空", field=field, value=value)
    if CSS_FORBIDDEN.search(value):
        reject_theme_value(rule="主题 token 包含不允许的 CSS 内容", field=field, value=value)
    normalized = value.strip()
    if name in _COLOR_TOKENS and not _COLOR_VALUE.fullmatch(normalized):
        reject_theme_value(rule="主题颜色 token 值无效", field=field, value=value)
    if name in _LENGTH_TOKENS and not _LENGTH_VALUE.fullmatch(normalized):
        reject_theme_value(rule="主题尺寸 token 值无效", field=field, value=value)
    if name == "--bt-background-image" and normalized != "none":
        reject_theme_value(
            rule="--bt-background-image 只能为 none；图片必须使用 background 字段", field=field, value=value,
        )
    if name == "--bt-background-repeat" and normalized not in {
        "no-repeat",
        "repeat",
        "repeat-x",
        "repeat-y",
        "space",
        "round",
    }:
        reject_theme_value(rule="主题背景重复 token 值无效", field=field, value=value)
    if name in {"--bt-background-position", "--bt-background-size"} and not (
        _POSITION_SIZE_VALUE.fullmatch(normalized)
    ):
        reject_theme_value(rule="主题背景布局 token 值无效", field=field, value=value)
    if name == "--bt-surface-backdrop-filter" and not (
        normalized == "none"
        or re.fullmatch(
            r"(?:(?:blur|saturate|brightness|contrast)\([0-9.]+(?:px|%|)?\)\s*)+",
            normalized,
        )
    ):
        reject_theme_value(rule="主题表面滤镜 token 值无效", field=field, value=value)


def validate_background_display_tokens(tokens: dict[str, str]) -> None:
    for name, value in tokens.items():
        if not value.strip() or CSS_FORBIDDEN.search(value):
            reject_theme_value(rule="背景展示参数包含不允许的 CSS 内容", field="background.display", value={name: value})
