from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.agents.custom_tools import CustomToolFactoryContext


LARGE_TEST_TARGET_LINE_INDEX = 1_200
LARGE_TEST_TARGET_VALUE = "BOXTEAM_MIDDLE_SECRET_7F3A9C"
LARGE_TEST_OUTPUT = "\n".join(
    (
        f"large-output-line-{index:04d}: retrieval-target={LARGE_TEST_TARGET_VALUE}"
        if index == LARGE_TEST_TARGET_LINE_INDEX
        else f"large-output-line-{index:04d}: {'x' * 40}"
    )
    for index in range(2_400)
)


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


def create_large_test_output_tool(context: CustomToolFactoryContext) -> BaseTool:
    """创建用于验证大工具输出物化链路的扩展工具。"""
    del context

    @tool("large_test_output")
    def large_test_output() -> str:
        """返回稳定且超过 ToolOutputStore 默认阈值的多行文本。"""
        return LARGE_TEST_OUTPUT

    return large_test_output
