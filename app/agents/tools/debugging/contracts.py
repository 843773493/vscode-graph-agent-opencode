from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DebugScope = Literal["local", "global", "all"]
VariableName = Annotated[str, Field(min_length=1)]


class _StrictDebugInput(BaseModel):
    """调试目标输入基类：拒绝一切未声明的模型参数。

    模型不能提供 session/thread、Inspector 端口、DAP 或 VS Code 内部字段；
    这些字段既不在 schema 中，也会被 ``extra="forbid"`` 明确拒绝，而不是静默丢弃。
    """

    model_config = ConfigDict(extra="forbid")


class _NoArgumentInput(_StrictDebugInput):
    """无参数调试目标的显式空 schema；多余参数同样明确拒绝。

    TODO: LangChain 的 ``BaseTool._to_args_and_kwargs`` 对“无字段 args_schema”会直接
    返回空参数，因此直接 ``ainvoke`` 的多余参数到不了本模型；模型面向的固定信封
    仍按公开模型的 extra="forbid" 明确拒绝这些参数。
    """


class StartDebuggingInput(_StrictDebugInput):
    fileFullPath: str = Field(
        description="要调试的源码路径；优先使用工作区相对路径，工作区内绝对路径会被自动归一化。"
    )
    workingDirectory: str = Field(
        description="调试目录；优先使用工作区相对路径，用 . 表示 workspace 根目录。"
    )
    testName: str | None = Field(
        default=None,
        description="可选的测试名称；当前 Node Inspector adapter 暂不支持单测试启动。",
    )
    configurationName: str | None = Field(
        default=None,
        description="可选的工作区 debug launch profile 名称。",
    )
    debugConfigurationId: str | None = Field(
        default=None,
        description="可选的当前会话调试方案 ID；省略时使用活动方案。",
    )


class CreateDebugConfigurationInput(_StrictDebugInput):
    name: str = Field(min_length=1, max_length=80, description="调试方案显示名。")
    fileFullPath: str = Field(
        description="目标 JavaScript 路径；优先使用工作区相对路径。"
    )
    workingDirectory: str = Field(
        description="目标程序目录；优先使用工作区相对路径，用 . 表示 workspace 根目录。"
    )
    configurationName: str | None = Field(
        default=None,
        description="可选的工作区 debug launch profile 名称。",
    )
    arguments: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="传给目标程序的启动参数。",
    )


class DebugConfigurationIdInput(_StrictDebugInput):
    debugConfigurationId: str = Field(
        min_length=1,
        description="当前会话中的调试方案 ID。",
    )


class BreakpointInput(_StrictDebugInput):
    fileFullPath: str = Field(description="源码路径；优先使用工作区相对路径。")
    line: int = Field(ge=1, description="从 1 开始的源码行号。")
    condition: str | None = Field(default=None, description="可选的条件表达式。")
    hitCondition: int | None = Field(
        default=None,
        ge=1,
        description="可选的命中次数；仅在本次目标进程第 N 次到达时暂停。",
    )


class RemoveBreakpointInput(_StrictDebugInput):
    fileFullPath: str = Field(description="源码路径；优先使用工作区相对路径。")
    line: int = Field(ge=1, description="从 1 开始的源码行号。")


class LogpointInput(_StrictDebugInput):
    fileFullPath: str = Field(description="源码路径；优先使用工作区相对路径。")
    line: int = Field(ge=1, description="从 1 开始的源码行号。")
    logMessage: str = Field(
        min_length=1,
        description="命中时记录的日志消息，使用 {expression} 插入运行时值。",
    )
    condition: str | None = Field(default=None, description="可选的条件表达式。")
    hitCondition: int | None = Field(
        default=None,
        ge=1,
        description="可选的命中次数；仅在本次目标进程第 N 次到达时输出。",
    )


class VariableNamesInput(_StrictDebugInput):
    scope: DebugScope | None = Field(
        default=None,
        description="变量范围：local、global 或 all。",
    )


class VariableValuesInput(_StrictDebugInput):
    variableNames: list[VariableName] = Field(
        min_length=1,
        max_length=50,
        description="要读取的变量名列表，不支持通配符。",
    )
    scope: DebugScope | None = Field(
        default=None,
        description="变量范围：local、global 或 all。",
    )


class EvaluateExpressionInput(_StrictDebugInput):
    expression: str = Field(min_length=1, description="当前暂停上下文中的表达式。")
