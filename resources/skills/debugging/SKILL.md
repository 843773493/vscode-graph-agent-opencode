---
name: debugging
description: 用户要求调试 JavaScript/Node.js 源码、设置断点、查看调用栈或变量、求值、单步执行和确认程序结束时使用。
allowed-tools: list_debug_configurations, create_debug_configuration, activate_debug_configuration, delete_debug_configuration, start_debugging, stop_debugging, step_over, step_into, step_out, continue_execution, pause_execution, restart_debugging, add_breakpoint, add_logpoint, remove_breakpoint, clear_all_breakpoints, list_breakpoints, list_variable_names, get_variables_values, evaluate_expression
---

# 源码调试工具

这是通过固定扩展入口 invoke_custom_tool 提供给 Agent 的源码级调试工具组。目标工具不会直接出现在 Agent 的模型工具列表中，也不是让模型连接 Inspector 的底层接口。所有动作都会进入 Agent 工具轨迹和调试 session 审计记录。

## 必须遵循的流程

1. 用户请求匹配本 Skill 后，先用 read_file 读取 .boxteam/bundled-skills/debugging/SKILL.md，再通过 invoke_custom_tool 执行调试动作。
2. 从用户请求或工作区文件中确认要调试的 JS 文件和工作目录。fileFullPath、workingDirectory 必须使用标准工作区相对路径；优先直接复用 ls、glob、grep 返回的路径。例如使用 `counter-entry.mjs`、`src/main.mjs`，workspace 根目录使用 `.`。不要添加开头的 `/`，也不要传入真实宿主机绝对路径；DeepAgents 内部的虚拟路径转换不属于模型参数协议。
3. 先调用 list_debug_configurations 读取当前会话的方案列表与活动方案，再调用 list_breakpoints 读取权威运行状态。一个会话可以保存多套方案，但同一时间只激活一套。人类可能已经切换方案、启动调试、设置断点或推进执行；若 state.status 是 running 或 paused，直接从该状态继续，不要为“取得控制权”而重启。只有没有活动运行时时，才按需创建/激活方案、添加断点和启动。
4. 用户只描述功能或目标而没有给出入口文件时，使用 glob、grep 和 read_file 检查工作区中的 JavaScript 文件、导入关系和实际可执行入口，不能根据文件名猜测。若现有方案的名称或目标文件都不匹配本次请求，先调用 create_debug_configuration 创建语义明确的具名方案，再向该方案添加入口和相关模块断点；不要先设置断点触发匿名方案。
5. 每次工具返回 state.status=paused 后，先根据最新 call_stack[0] 的文件、函数和行号在对话中向用户说明该处代码的作用，再执行本次暂停所需的变量读取或 evaluate_expression，最后才能 continue_execution 或 step_*。不得提前写好尚未实际命中的下一断点说明。
6. 用户要求把暂停位置中的计数变量加一时，调用 evaluate_expression 并使用真实变量名构造 `<变量> += 1`；确认工具返回的求值结果后再继续。该表达式只改变目标进程的当前暂停帧，不得使用 edit_file 修改源码来伪造结果。调试控制台会展示求值历史。
7. 只有工具返回的 state.status 为 paused 时，才读取变量或调用 evaluate_expression、step_*。不要根据猜测的行号或旧快照继续执行。
8. 每次单步、继续、暂停或重启后，都以工具返回的完整 state 为准；不要向用户声称已经命中断点，除非返回了 paused 和调用栈。
9. 调试结束必须观察到 state.status 为 exited 或明确的 failed，再向用户报告结果。最后一次 continue_execution 或 step_* 如果暂时返回 running，调用 list_breakpoints 重新读取权威状态，不能把 running 直接说成已结束。失败不能伪装成正常结束。
10. evaluate_expression 可能执行有副作用的代码。仅在用户请求或已有确认策略允许时调用，并在解释中说明表达式和实际返回值。
11. add_breakpoint 可使用 condition 和正整数 hitCondition；后者按本次目标进程的命中次数触发，重启后从 0 重新计数。add_logpoint 使用 logMessage 记录信息但不暂停，消息中的 `{expression}` 会在命中位置求值；只有需要观察而不打断执行时使用日志点。
12. 不请求、不拼接、不向用户传递 Inspector URL、Inspector 端口、WebSocket 地址、threadId 或 frameId。会话、调用栈顶层 frame 和运行时连接由后端绑定。
13. 通用 state 中的 call_stack 只返回栈结构，不附带变量值。先用 list_variable_names 发现名称，再用 get_variables_values 显式请求最多 50 个必要变量；不要尝试通过其他工具的 state 绕过此限制。
14. get_variables_values 和 evaluate_expression 会把疑似密钥、令牌、密码或连接凭据替换成 `<redacted: possible secret>`。看到 redaction_notice 后只能改用类型、长度、是否为空等调试判断，不能尝试编码、切片或其他表达式绕过脱敏。
15. 人类和 Agent 始终共享当前 session 的同一调试运行时，不存在接管、交接或控制模式。人类可能在你的两次工具调用之间点击继续、暂停、单步、停止、增删断点，也可能在终端输入、修改源码或发送新消息。每次都接受工具返回的最新 state；如果状态已推进，基于新位置继续分析，不要回滚或抢占。
16. state.requires_restart 为 true 时，说明磁盘源码已不同于当前 Node 进程加载的版本。先检查 source_changed_paths 和断点 relocation_status；`pending_update` 或 `source_deleted` 必须请用户检查或重新设置，不能继续声称旧断点仍准确。需要运行新源码时调用 restart_debugging。
17. 调试入口、工作目录、参数、profile 和断点保存在当前会话的活动方案。启动已有方案时，方案 JSON 是这些参数的唯一权威来源；`start_debugging` 的文件和工作目录只用于尚无方案时创建首套方案。用户从 Web 启动后再发消息要求继续调试时，先复用该状态；不要把会话选择写回 Workspace 默认配置。
18. 本工具只用于替用户调试工作区中的目标程序，不得用于暂停或检查 BoxTeam Agent 自身的思考、LLM 请求和工具循环。本工具组没有这类能力，不要尝试通过 Job 控制模拟。
19. 每套方案是独立版本化 JSON，使用工作区相对路径且不包含 session ID、PID、Inspector 地址或动作历史。用户可以把 `debug/node/configurations/<configuration_id>.json` 复制到另一会话；不要把 `manifest.json` 当成可移植方案。

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
- breakpoints.relocation_status：`current`、`relocated`、`pending_update` 或 `source_deleted`；后两者不会安装到猜测位置。
- variables：仅由 get_variables_values 对显式请求的名称返回，并可能包含脱敏占位符。
- last_evaluation：仅在后端确认暂停并完成求值后返回，疑似凭据会被脱敏。
- actions：按时间顺序记录工具动作、工具调用 ID、结果和时间。
- configuration_revision：当前活动方案修订；requires_restart 和 source_changed_paths 表示运行中的源码已落后于磁盘文件。

如果工具返回 ok: false，先读取 error.code 和 state 决定下一步；不要用自然语言覆盖工具的失败事实。
