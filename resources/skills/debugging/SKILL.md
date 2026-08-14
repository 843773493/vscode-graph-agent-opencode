---
name: debugging
description: 用户要求调试 JavaScript/Node.js 源码、设置断点、查看调用栈或变量、求值、单步执行和确认程序结束时使用。
allowed-tools: list_debug_configurations, create_debug_configuration, activate_debug_configuration, delete_debug_configuration, start_debugging, stop_debugging, step_over, step_into, step_out, continue_execution, pause_execution, restart_debugging, add_breakpoint, add_logpoint, remove_breakpoint, clear_all_breakpoints, list_breakpoints, list_variable_names, get_variables_values, evaluate_expression
---

# 源码调试工具

这是通过固定扩展入口 invoke_custom_tool 提供给 Agent 的源码级调试工具组。目标工具不会直接出现在 Agent 的模型工具列表中，也不是让模型连接 Inspector 的底层接口。所有动作都会进入 Agent 工具轨迹和调试 session 审计记录。

## 给模型的最短决策流程

你是在替用户观察和控制目标 JavaScript 程序，不是在管理 Inspector 连接。先看状态再行动：每次动作都以工具返回的最新 `state` 为准，优先少调用、少重复和少假设。

1. 用户请求匹配本 Skill 后，先用 `read_file` 读取 `.boxteam/bundled-skills/debugging/SKILL.md`，再通过 `invoke_custom_tool` 调用目标调试工具。
2. 首次进入调试时，先调用一次 `list_debug_configurations`。它的返回 `state` 同时包含活动方案、方案列表、断点和运行状态；只有状态可能已被人类改变、返回不完整或需要单独确认断点时，才追加 `list_breakpoints`。一个会话可保存多套方案，但同时只激活一套。
3. 根据这次返回做分支：
   - `running` 或 `paused`：复用当前运行时，直接处理用户目标；不要为了“取得控制权”重启，也不要重新创建方案。
   - `idle`、`exited` 或 `failed`：如果用户要继续一次新的运行，选择匹配的活动方案；没有匹配方案时才创建具名方案、设置断点并启动。
   - 没有入口文件时，使用 `glob`、`grep`、`read_file` 确认实际可执行入口和导入关系，不凭文件名猜测。
4. 方案匹配时，方案 JSON 是入口、工作目录、profile、参数和断点的权威来源。`start_debugging` 的必填路径只用于满足调用契约，不要用临时参数偷偷覆盖已有方案；需要改变方案时先显式创建或激活正确方案。
5. 每次返回后先看 `state.status`，再决定下一步：
   - `paused`：依据最新 `call_stack[0]` 的文件、函数和行号向用户解释当前代码作用；只有用户目标或分析确实需要时才读取变量、求值或单步，然后再继续。
   - `running`：不要读取变量、求值或单步；如用户要求停下，调用 `pause_execution`，否则等待下一次状态或用户消息。
   - `exited`：程序已结束；`failed`：调试失败。两者都不能描述成暂停或成功运行。
6. 用户要求把暂停位置的计数变量加一时，先确认真实变量名，再用 `evaluate_expression` 执行 `<变量> += 1`，检查返回值后继续。它只改变目标进程内存，不修改源码；表达式有副作用时遵守确认策略。
7. 不要预先解释尚未命中的断点。每次单步、继续、暂停或重启后都只陈述返回的实际位置；没有 `paused` 和调用栈就不要声称命中了断点。
8. 源码变化是“断点失效提醒”，不是“调试阻断”：`pending_update`/`source_deleted` 断点不会自动重定位、不会安装到猜测位置，但 `continue_execution`、`pause_execution`、`step_*` 和 `stop_debugging` 仍可调用。需要调试新源码时，先检查当前文件，移除旧路径/旧行号的失效断点，再按当前行重新添加；确认后才按需 `restart_debugging`。
9. `start_debugging` 返回顶层 `invalid_breakpoints` 时，记录并告诉用户哪些断点失效，但继续检查是否有其他有效断点或程序状态，不要因为该字段存在就停止调试。调试结束的最后一个成功控制工具在状态为 `exited`/`failed` 时也会返回该字段；普通 `list_breakpoints` 不会重复返回顶层字段，仍要查看 `state.breakpoints`。
10. 最后一个 `continue_execution` 或单步若返回 `running`，不能直接说已结束；用 `list_breakpoints` 获取最新 `state`。确认 `exited`/`failed` 后再报告结果，并保留失效断点提醒。

## 并发、人类操作和安全边界

- 人类和 Agent 始终共享同一个 session 调试运行时，不存在接管、交接或权限模式；不需要先交接控制权。人类可能在两次工具调用之间继续、暂停、单步、停止、增删断点、在终端输入、修改源码或发送新消息。
- 用户新消息不是“等待授权”的信号，而是对当前最新状态的新指令：先读取或使用下一次工具返回的权威状态，接受已经发生的推进，不回滚、不抢占、不重启来夺回控制权。
- `call_stack` 只提供栈结构，不包含变量值。先用 `list_variable_names`，再用 `get_variables_values` 读取最多 50 个必要变量。
- 变量和求值中的疑似密钥、令牌、密码或凭据会替换为 `<redacted: possible secret>`；看到 `redaction_notice` 后只能使用类型、长度或空值判断，不得尝试编码、切片或其他表达式绕过脱敏。
- 不请求、不拼接、不向用户传递 Inspector URL、Inspector 端口、WebSocket 地址、`threadId` 或 `frameId`。本工具只调试用户目标程序，不调试 BoxTeam Agent 自身的思考、LLM 请求或工具循环。
- 每套方案是独立版本化 JSON，使用标准工作区相对路径且不包含 session ID、PID、Inspector 地址或动作历史；方案文件可复制到其他会话。

## 断点类型

`add_breakpoint` 支持普通、条件和命中次数断点；`hitCondition` 按本次目标进程计数，重启后重新计数。`add_logpoint` 只记录 `logMessage`（可用 `{expression}` 插入值）而不暂停；需要观察而不打断执行时优先使用日志点。

## 工具参数契约

以下对象是 invoke_custom_tool.arguments 中目标工具的参数 JSON Schema。固定入口的外层 schema 由 Agent 工具定义提供，不要把目标工具名称注册成模型的直接工具。

{
  "tool_name": "list_debug_configurations",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "create_debug_configuration",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["name", "fileFullPath", "workingDirectory"],
    "properties": {
      "name": {"type": "string", "minLength": 1, "maxLength": 80},
      "fileFullPath": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；不能以 / 开头"},
      "workingDirectory": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；使用 . 表示 workspace 根目录"},
      "configurationName": {"type": ["string", "null"]},
      "arguments": {"type": "array", "maxItems": 64, "items": {"type": "string"}}
    }
  }
}

{
  "tool_name": "activate_debug_configuration",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["debugConfigurationId"],
    "properties": {"debugConfigurationId": {"type": "string", "minLength": 1}}
  }
}

{
  "tool_name": "delete_debug_configuration",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["debugConfigurationId"],
    "properties": {"debugConfigurationId": {"type": "string", "minLength": 1}}
  }
}

{
  "tool_name": "start_debugging",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["fileFullPath", "workingDirectory"],
    "properties": {
      "fileFullPath": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；不能以 / 开头"},
      "workingDirectory": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；使用 . 表示 workspace 根目录"},
      "testName": {"type": ["string", "null"]},
      "configurationName": {"type": ["string", "null"]},
      "debugConfigurationId": {"type": ["string", "null"]}
    }
  }
}

testName 目前不支持 Node 单测试启动；不要为了满足请求而猜测测试命令。需要使用 launch profile 时，把已存在的 profile 名称放入 configurationName。

{
  "tool_name": "stop_debugging",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "step_over",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "step_into",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "step_out",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "continue_execution",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "pause_execution",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "restart_debugging",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "add_breakpoint",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["fileFullPath", "line"],
    "properties": {
      "fileFullPath": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；不能以 / 开头"},
      "line": {"type": "integer", "minimum": 1},
      "condition": {"type": ["string", "null"]},
      "hitCondition": {"type": ["integer", "null"], "minimum": 1}
    }
  }
}

{
  "tool_name": "add_logpoint",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["fileFullPath", "line", "logMessage"],
    "properties": {
      "fileFullPath": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；不能以 / 开头"},
      "line": {"type": "integer", "minimum": 1},
      "logMessage": {"type": "string", "minLength": 1, "description": "使用 {expression} 插入运行时值"},
      "condition": {"type": ["string", "null"]},
      "hitCondition": {"type": ["integer", "null"], "minimum": 1}
    }
  }
}

{
  "tool_name": "remove_breakpoint",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["fileFullPath", "line"],
    "properties": {
      "fileFullPath": {"type": "string", "minLength": 1, "description": "标准工作区相对路径；不能以 / 开头"},
      "line": {"type": "integer", "minimum": 1}
    }
  }
}

{
  "tool_name": "clear_all_breakpoints",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "list_breakpoints",
  "arguments_schema": {"type": "object", "additionalProperties": false, "properties": {}}
}

{
  "tool_name": "list_variable_names",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "scope": {"type": ["string", "null"], "enum": ["local", "global", "all", null]}
    }
  }
}

{
  "tool_name": "get_variables_values",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["variableNames"],
    "properties": {
      "variableNames": {
        "type": "array",
        "minItems": 1,
        "maxItems": 50,
        "items": {"type": "string", "minLength": 1}
      },
      "scope": {"type": ["string", "null"], "enum": ["local", "global", "all", null]}
    }
  }
}

{
  "tool_name": "evaluate_expression",
  "arguments_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["expression"],
    "properties": {"expression": {"type": "string", "minLength": 1}}
  }
}

## 状态和审计

所有工具都返回 JSON 文本，包含 ok、message 或稳定的 error.code，成功和失败都尽可能包含当前完整 state。重点字段是：

- status：idle、starting、running、paused、exited 或 failed。
- call_stack：后端确认的调用栈结构；通用 state 不携带变量值，顶层 frame 是显式变量读取和表达式求值的上下文。
- breakpoints：断点的相对路径、行号、condition、hit_condition、log_message 和验证状态。
- breakpoints.relocation_status：`current`、`pending_update` 或 `source_deleted`；后两者不会安装到猜测位置，也不会自动恢复。源码变化后的断点会保留原请求行号，必须显式重新设置后才恢复为 `current`。
- variables：仅由 get_variables_values 对显式请求的名称返回，并可能包含脱敏占位符。
- last_evaluation：仅在后端确认暂停并完成求值后返回，疑似凭据会被脱敏。
- actions：按时间顺序记录工具动作、工具调用 ID、结果和时间。
- configuration_revision：当前活动方案修订；requires_restart 和 source_changed_paths 表示运行中的源码已落后于磁盘文件。

`start_debugging` 成功结果会额外返回顶层 `invalid_breakpoints`；当最后一个成功控制工具使状态变为 `exited` 或 `failed` 时，也会返回该字段。数组中的每项包含 `path`、`line`、`original_line`、`relocation_status` 和 `relocation_message`。其他工具直接查看 `state.breakpoints` 中的失效状态即可，不要因为缺少顶层数组而认为断点有效。

如果工具返回 ok: false，先读取 error.code 和 state 决定下一步；不要用自然语言覆盖工具的失败事实。
