from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.agents.custom_tools import CustomToolFactoryContext


def create_test_tool() -> BaseTool:
    """创建一个用于测试工具调用链路的工具。"""

    @tool("test_tool")
    def test_tool() -> str:
        """用于验证工具调用流程是否正确。"""
        return "2333"

    return test_tool


def create_test_tool_2(context: CustomToolFactoryContext) -> BaseTool:
    """创建一个只能通过固定扩展入口调用的测试工具。"""
    del context

    @tool("test_tool_2")
    def test_tool_2() -> str:
        """用于验证固定扩展入口调用自定义工具的流程是否正确。"""
        return "4568"

    return test_tool_2
