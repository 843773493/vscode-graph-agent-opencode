from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core.path_utils import resolve_boxteam_home
from app.core.storage_migration import migrate_user_storage_layout


DEVELOPMENT_ASSETS_ENV = "BOXTEAM_INSTALL_DEVELOPMENT_ASSETS"
GATEWAY_E2E_WORKSPACE_ENV = "BOXTEAM_ENABLE_GATEWAY_E2E_WORKSPACE"
SSH_KEY_NAME = "boxteam_gateway_e2e_ed25519"
SSH_HOST_ALIAS = "boxteam-gateway-e2e"
SSH_BLOCK_BEGIN = "# BEGIN BOXTEAM GATEWAY E2E"
SSH_BLOCK_END = "# END BOXTEAM GATEWAY E2E"


def _provider(
    provider_id: str,
    endpoint: str,
    model: str,
    api_key_env: str,
    provider_kind: str,
    *,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": provider_id,
        "endpoint": endpoint,
        "model": model,
        "api_key": f"${{{api_key_env}}}",
        "custom_llm_provider": provider_kind,
    }
    if capabilities:
        result["capabilities"] = capabilities
    return result


def _agent(
    name: str,
    description: str,
    system_prompt: str,
    *,
    primary_provider: str = "primary",
    fallback_providers: list[str] | None = None,
    tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "description": description,
        "language": "zh-CN",
        "instructions": {"system_prompt": system_prompt},
        "model": {
            "primary_provider": primary_provider,
            "fallback_providers": fallback_providers
            if fallback_providers is not None
            else ["backup_1", "backup_2", "backup_3"],
        },
    }
    if tools is not None:
        result["tools"] = tools
    return result


def _default_custom_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "read_session_recent_text_messages",
            "factory": "app.agents.tools.session_history:create_read_session_recent_text_messages_tool",
        },
        {
            "name": "grep_session_context_jsonl",
            "factory": "app.agents.tools.session_history:create_grep_session_context_jsonl_tool",
        },
        {
            "name": "read_session_context_jsonl",
            "factory": "app.agents.tools.session_history:create_read_session_context_jsonl_tool",
        },
        {
            "name": "web_search",
            "factory": "app.agents.tools.web:create_web_search_tool",
        },
        {
            "name": "fetch_webpage",
            "factory": "app.agents.tools.web:create_fetch_webpage_tool",
            "options": {
                "embedding": {
                    "provider_id": "backup_2",
                    "model": "openai/text-embedding-3-small",
                }
            },
        },
        *[
            {
                "name": name,
                "factory": f"app.agents.tools.browser:create_{factory_suffix}_tool",
            }
            for name, factory_suffix in (
                ("openBrowserPage", "open_browser_page"),
                ("readPage", "read_page"),
                ("navigatePage", "navigate_page"),
                ("clickElement", "click_element"),
                ("typeInPage", "type_in_page"),
                ("hoverElement", "hover_element"),
                ("dragElement", "drag_element"),
                ("handleDialog", "handle_dialog"),
                ("screenshotPage", "screenshot_page"),
                ("runPlaywrightCode", "run_playwright_code"),
            )
        ],
    ]


def build_boxteam_config(
    *,
    development_assets: bool,
    gateway_e2e_workspace_enabled: bool = False,
) -> dict[str, Any]:
    """构建用户级配置；开发专属能力只在安装开关启用时写入。"""
    if gateway_e2e_workspace_enabled and not development_assets:
        raise ValueError("启用 Gateway E2E 工作区前必须先安装开发资产")
    default_tools = {
        "denylist": [],
        "confirmation_required": [],
        "custom": _default_custom_tools(),
    }
    if development_assets:
        default_tools["custom"].insert(
            0,
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
            },
        )

    config: dict[str, Any] = {
        "$schema": "./config.schema.jsonc",
        "llm": {
            "providers": [
                _provider(
                    "primary",
                    "https://opencode.ai/zen/v1",
                    "big-pickle",
                    "OPENCODE_ZEN_API_KEY",
                    "openai",
                ),
                _provider(
                    "backup_1",
                    "https://api.kilo.ai/api/gateway",
                    "openrouter/free",
                    "KILO_API_KEY",
                    "openai",
                ),
                _provider(
                    "backup_2",
                    "https://openrouter.ai/api/v1",
                    "openrouter/free",
                    "OPENROUTER_API_KEY",
                    "openrouter",
                ),
                _provider(
                    "backup_3",
                    "https://www.cctq.ai/v1",
                    "gpt-5.6-luna",
                    "CCTQ_API_KEY",
                    "openai",
                    capabilities=["image_input"],
                ),
            ]
        },
        "logger": {"level": "info"},
        "default_agent": "default",
        "agents": {
            "default": _agent(
                "Workspace Assistant",
                "工作区默认助手，负责通用问答、代码解释、开发辅助和文档问答。",
                "你是面向工程团队的工作区助手。优先提供准确、可执行、可验证的回答；信息不足时明确指出缺失信息。",
                tools=default_tools,
            ),
            "coder": _agent(
                "Coding Assistant",
                "专门用于代码实现、重构和修复问题。",
                "你是工程实践导向的编程助手。输出代码时优先保证正确性、可读性和可验证性。",
                tools={
                    "denylist": ["send_message_to_session", "edit_file"],
                    "confirmation_required": [],
                },
            ),
            "reviewer": _agent(
                "Code Reviewer",
                "专门用于代码审查、风险检查和设计评估。",
                "你是代码审查助手。优先指出正确性、架构、安全性和可维护性问题，并给出具体依据。",
            ),
            "researcher": _agent(
                "Research Assistant",
                "专门用于资料检索、文档总结和方案调研。",
                "你是调研助手。优先基于可验证来源归纳结论，并明确不确定点和假设。",
            ),
        },
        "gateway": {"workspaces": []},
        "development": {"test_tools": development_assets},
    }
    if development_assets:
        config["gateway"]["workspaces"].append(
            {
                "enabled": gateway_e2e_workspace_enabled,
                "kind": "ssh",
                "name": "Gateway E2E Docker Workspace",
                "host": "127.0.0.1",
                "port": 22222,
                "username": os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_USER", "root"),
                "private_key_path": f"~/.ssh/{SSH_KEY_NAME}",
                "remote_backend_host": "127.0.0.1",
                "remote_backend_port": 8010,
                "remote_workspace_path": "/root/.boxteams/boxteam_workspace",
                "activate": False,
            }
        )
    return config


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"拒绝覆盖符号链接: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary_path.chmod(mode)
    os.replace(temporary_path, path)


def write_boxteam_config(
    path: Path,
    *,
    development_assets: bool,
    gateway_e2e_workspace_enabled: bool = False,
) -> None:
    payload = build_boxteam_config(
        development_assets=development_assets,
        gateway_e2e_workspace_enabled=gateway_e2e_workspace_enabled,
    )
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path.expanduser().resolve(), content, 0o600)


def install_config_schema(*, project_root: Path, config_path: Path) -> Path:
    source = project_root / "configs" / "config.jsonc"
    if not source.is_file():
        raise FileNotFoundError(f"配置 schema 不存在: {source}")
    target = config_path.expanduser().resolve().parent / "config.schema.jsonc"
    _atomic_write(target, source.read_bytes(), 0o600)
    return target


def _replace_managed_ssh_block(existing: str, block: str) -> str:
    begin_count = existing.count(SSH_BLOCK_BEGIN)
    end_count = existing.count(SSH_BLOCK_END)
    if begin_count != end_count or begin_count > 1:
        raise RuntimeError(
            "~/.ssh/config 中的 BoxTeam 托管块损坏或重复，"
            f"begin={begin_count} end={end_count}"
        )
    if begin_count == 1:
        start = existing.index(SSH_BLOCK_BEGIN)
        end = existing.index(SSH_BLOCK_END, start) + len(SSH_BLOCK_END)
        updated = existing[:start] + block + existing[end:]
    else:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        updated = existing + separator + block
    return updated.rstrip("\n") + "\n"


def install_development_ssh_assets(*, project_root: Path, home: Path) -> None:
    source_root = project_root / "asset" / "gateway_ssh"
    private_source = source_root / SSH_KEY_NAME
    public_source = source_root / f"{SSH_KEY_NAME}.pub"
    if not private_source.is_file() or not public_source.is_file():
        raise FileNotFoundError(f"Gateway E2E SSH 密钥不完整: {source_root}")

    ssh_root = home.expanduser().resolve() / ".ssh"
    ssh_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ssh_root.chmod(0o700)
    private_target = ssh_root / SSH_KEY_NAME
    public_target = ssh_root / f"{SSH_KEY_NAME}.pub"
    _atomic_write(private_target, private_source.read_bytes(), 0o600)
    _atomic_write(public_target, public_source.read_bytes(), 0o644)

    config_path = ssh_root / "config"
    if config_path.is_symlink():
        raise RuntimeError(f"拒绝修改符号链接 SSH config: {config_path}")
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    ssh_user = os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_USER", "root").strip()
    if not ssh_user:
        raise ValueError("BOXTEAM_GATEWAY_E2E_SSH_USER 不能为空")
    block = "\n".join(
        (
            SSH_BLOCK_BEGIN,
            f"Host {SSH_HOST_ALIAS}",
            "  HostName 127.0.0.1",
            "  Port 22222",
            f"  User {ssh_user}",
            f"  IdentityFile ~/.ssh/{SSH_KEY_NAME}",
            "  IdentitiesOnly yes",
            SSH_BLOCK_END,
        )
    )
    updated = _replace_managed_ssh_block(existing, block)
    if updated != existing:
        _atomic_write(config_path, updated.encode("utf-8"), 0o600)
    else:
        config_path.chmod(0o600)


def _environment_flag(name: str) -> bool:
    raw_value = os.environ.get(name, "0").strip()
    if raw_value not in {"0", "1"}:
        raise ValueError(f"{name} 只允许 0 或 1，实际值: {raw_value!r}")
    return raw_value == "1"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成并安装 BoxTeam 用户级配置")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()

    development_assets = _environment_flag(DEVELOPMENT_ASSETS_ENV)
    gateway_e2e_workspace_enabled = _environment_flag(GATEWAY_E2E_WORKSPACE_ENV)
    boxteam_home = resolve_boxteam_home(args.home)
    configured_default_workspace = os.environ.get(
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT"
    )
    default_workspace_root = Path(
        configured_default_workspace or boxteam_home / "boxteam_workspace"
    ).expanduser().resolve()
    migrate_user_storage_layout(
        home=args.home.expanduser().resolve(),
        boxteam_home=boxteam_home,
        default_workspace_root=default_workspace_root,
    )
    output = args.output or boxteam_home / "config" / "boxteam.jsonc"
    write_boxteam_config(
        output,
        development_assets=development_assets,
        gateway_e2e_workspace_enabled=gateway_e2e_workspace_enabled,
    )
    install_config_schema(
        project_root=args.project_root.expanduser().resolve(),
        config_path=output,
    )
    if development_assets:
        install_development_ssh_assets(
            project_root=args.project_root.expanduser().resolve(),
            home=args.home,
        )
    print(
        json.dumps(
            {
                "config_path": str(output.expanduser().resolve()),
                "development_assets": development_assets,
                "gateway_e2e_workspace_enabled": gateway_e2e_workspace_enabled,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
