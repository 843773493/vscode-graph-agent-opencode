"""主题语义拒绝保留规则，原输入只进入诊断。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core import failures
from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.gateway.theme.resolver import resolve_theme


@pytest.fixture(params=["/private/theme-secret", r"C:\private\theme-secret", r"\\host\private\theme-secret"],
                ids=["posix", "drive", "unc"])
def native_path(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def config_with_invalid_theme():
    def build(tokens: dict[str, str] | None = None, background: dict[str, object] | None = None) -> GatewayConfig:
        return GatewayConfig(custom_themes=(ConfiguredTheme(
            "custom", "自定义", "warm", "light", tokens or {}, background,
        ),))
    return build


@pytest.mark.parametrize("kind,rule", [
    ("unknown", "未知主题 token"), ("color", "颜色 token 值无效"),
    ("length", "尺寸 token 值无效"), ("repeat", "重复 token 值无效"),
    ("layout", "布局 token 值无效"), ("filter", "滤镜 token 值无效"),
    ("css", "不允许的 CSS 内容"),
])
def test_token_errors_do_not_copy_keys_or_values(
    config_with_invalid_theme, native_path: str, kind: str, rule: str,
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    tokens = {
        "unknown": {native_path: "red"}, "color": {"--bt-accent": native_path},
        "length": {"--bt-radius-large": native_path}, "repeat": {"--bt-background-repeat": native_path},
        "layout": {"--bt-background-size": native_path + "?invalid"},
        "filter": {"--bt-surface-backdrop-filter": native_path},
        "css": {"--bt-accent": f"url({native_path})"},
    }[kind]
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match=rule) as caught:
        resolve_theme("custom", config=config_with_invalid_theme(tokens=tokens), gateway_root=tmp_path)
    assert "theme-secret" not in str(caught.value)
    assert "stage=theme-validate" in str(caught.value)
    cause = caught.value.__cause__
    assert isinstance(cause, ValueError) and "theme-secret" in str(cause)
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    assert "theme-secret" in caplog.text
    resolved = resolve_theme("custom", config=config_with_invalid_theme(tokens={"--bt-accent": "#123456"}), gateway_root=tmp_path)
    assert resolved.tokens["--bt-accent"] == "#123456"


@pytest.mark.parametrize("field,rule", [("type", "不支持的主题背景类型"), ("appearance", "不支持的背景外观")])
def test_background_errors_do_not_copy_original_values(
    config_with_invalid_theme, native_path: str, field: str, rule: str,
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    background = {"type": "remote", "url": "https://example.test/background.png", field: native_path}
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match=rule) as caught:
        resolve_theme("custom", config=config_with_invalid_theme(background=background), gateway_root=tmp_path)
    assert "theme-secret" not in str(caught.value) and "stage=theme-validate" in str(caught.value)
    assert caught.value.__cause__ is not None and "theme-secret" in str(caught.value.__cause__)
    assert "theme-secret" in caplog.text


def test_theme_validation_survives_diagnostic_sink_failure(
    config_with_invalid_theme, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    errors: list[BaseException] = []

    def fail_log(_message: str, *_args: object, exc_info: BaseException) -> None:
        errors.append(exc_info)
        raise OSError("/private/theme-log-secret")

    monkeypatch.setattr(failures.logger, "error", fail_log)
    with pytest.raises(ValueError, match="颜色 token 值无效") as caught:
        resolve_theme("custom", config=config_with_invalid_theme(tokens={"--bt-accent": "/private/theme-secret"}), gateway_root=tmp_path)
    assert "theme-secret" not in str(caught.value) and "theme-log-secret" not in str(caught.value)
    assert "diagnostic_sink_error=OSError" in str(caught.value)
    assert len(errors) == 1 and caught.value.__cause__ is errors[0]
    assert "theme-log-secret" in "".join(errors[0].__notes__)
