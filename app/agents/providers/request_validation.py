"""最终 Provider 请求的纯值边界校验，不读取工作区或重写封存正文。"""

from __future__ import annotations

from collections.abc import Mapping

from app.agents.middleware_prompts import (
    RUNTIME_IDENTITY_HEADER,
    RUNTIME_WORKSPACE_SCOPE_FIELD,
)
from app.domain.itemized.epoch.source_protocol import (
    SOURCE_PROTOCOL,
    parse_source_blocks,
)


def reject_sealed_overrides(
    params: Mapping[str, object], forbidden: frozenset[str], *, protocol: str,
) -> None:
    """参数不得覆盖封存正文；出现即网络前失败。"""
    overridden = sorted(key for key in params if key in forbidden)
    if overridden:
        raise ValueError(
            f"source-mismatch: {protocol} provider 参数覆盖 sealed context: "
            + ",".join(overridden)
        )


def _validate_runtime_workspace_scope(content: object) -> None:
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            text = block.get("text") if isinstance(block, Mapping) else block
            if isinstance(text, str):
                texts.append(text)
        content = "\n".join(texts)
    if not isinstance(content, str):
        return
    # 最终 wire 已有显式协议标识；仅展开这一层软件包头，不递归解释 body。
    if content.startswith('{"protocol":"' + SOURCE_PROTOCOL + '","sources":'):
        for source in parse_source_blocks(content):
            _validate_scope_text(source.body)
        return
    _validate_scope_text(content)


def _validate_scope_text(content: object) -> None:
    if not isinstance(content, str):
        return
    if RUNTIME_IDENTITY_HEADER not in content and RUNTIME_WORKSPACE_SCOPE_FIELD not in content:
        return
    scopes = [
        line.removeprefix(RUNTIME_WORKSPACE_SCOPE_FIELD)
        for line in content.splitlines() if line.startswith(RUNTIME_WORKSPACE_SCOPE_FIELD)
    ]
    if scopes != ["`.`"]:
        raise ValueError(
            "real-path-not-allowed: R02 系统工作区范围必须为相对路径 `.`；"
            "已封存的真实根目录不能发送给 Provider，请建立新的上下文 epoch"
        )


def validate_provider_system_scope(payload: Mapping[str, object]) -> None:
    """校验两协议最终 system/developer/instructions 中的 R02 工作区范围。

    只解释已登记的 R02 字段；普通用户正文、代码和 HTTP 路径不属于此合同。
    历史封存内容原样拒绝，禁止改写、脱敏或截断后发送。
    """
    _validate_runtime_workspace_scope(payload.get("instructions"))
    for field in ("messages", "input"):
        messages = payload.get(field)
        if not isinstance(messages, list):
            continue
        for message in messages:
            if isinstance(message, Mapping) and message.get("role") in {"system", "developer"}:
                _validate_runtime_workspace_scope(message.get("content"))
