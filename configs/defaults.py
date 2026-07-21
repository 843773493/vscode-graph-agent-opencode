from __future__ import annotations

import os
from typing import Any


def _provider(
    provider_id: str,
    endpoint: str,
    model: str,
    api_key_env: str,
    provider_kind: str,
    *,
    api_mode: str = "chat_completions",
    capabilities: list[str] | None = None,
    request_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": provider_id,
        "endpoint": endpoint,
        "model": model,
        "api_key": f"${{{api_key_env}}}",
        "custom_llm_provider": provider_kind,
        "api_mode": api_mode,
    }
    if capabilities:
        result["capabilities"] = capabilities
    if request_options:
        result["request_options"] = request_options
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
            "fallback_providers": (
                fallback_providers
                if fallback_providers is not None
                else ["backup_1", "backup_2", "backup_3"]
            ),
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
    """构建用户级配置；开发专属能力只在显式请求时写入。"""
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
                    capabilities=["reasoning_content_replay"],
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
                    api_mode="responses",
                    capabilities=["image_input"],
                    request_options={
                        "overrides": {
                            "reasoning": {
                                "effort": "high",
                                "summary": "auto",
                            }
                        }
                    },
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
                    "denylist": ["edit_file"],
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
        config["mcp"] = {
            "servers": {
                "tui-mcp": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["--yes", "tui-mcp"],
                }
            }
        }
        config["gateway"]["workspaces"].append(
            {
                "enabled": gateway_e2e_workspace_enabled,
                "kind": "remote_gateway",
                "name": "Gateway E2E Docker Gateway",
                "host": "127.0.0.1",
                "port": 22222,
                "username": os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_USER", "boxteam"),
                "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
                "remote_gateway_port": 8014,
                "activate": False,
            }
        )
    return config
