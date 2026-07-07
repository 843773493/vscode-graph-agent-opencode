from __future__ import annotations

from langchain_core.tools import BaseTool, tool


def create_test_tool() -> BaseTool:
    """创建一个用于测试工具调用链路的工具。"""
    @tool("test_tool")
    def test_tool() -> str:
        """用于验证工具调用流程是否正确。"""
        return "2333"

    return test_tool
