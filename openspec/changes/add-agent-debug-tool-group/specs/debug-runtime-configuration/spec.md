## Purpose

为源码调试工具提供可验证、可分层覆盖的工作区运行配置，统一管理调试 adapter、启动 profile、Node Inspector 和未来 debugpy 的端口与执行边界，同时避免把基础设施细节暴露给 Agent。

## ADDED Requirements

### Requirement: Workspace configuration provides a debug namespace

Workspace 有效配置 SHALL 支持可选的 `runtime.debug` 命名空间，并保持现有配置合并顺序：内置默认配置、用户级配置、用户本地覆盖和工作区 `.boxteam/workspace.jsonc` 覆盖。新增字段 SHALL 使用当前配置 schema 校验；未知的 debug 字段不得静默忽略。

`runtime.debug` SHALL 支持以下配置结构：

```jsonc
{
  "runtime": {
    "debug": {
      "enabled": true,
      "default_adapter": "node_inspector",
      "command_timeout_seconds": 10,
      "node": {
        "inspector_host": "127.0.0.1",
        "inspector_port": 0,
        "executable": ""
      },
      "python": {
        "adapter": "debugpy",
        "debugpy_host": "127.0.0.1",
        "debugpy_port": 0
      },
      "launch_profiles": {
        "node-default": {
          "adapter": "node_inspector",
          "runtime": "node",
          "program": "",
          "working_directory": "",
          "args": []
        }
      }
    }
  }
}
```

#### Scenario: Existing configuration without debug settings remains valid

- **WHEN** 工作区使用没有 `runtime.debug` 的既有有效配置启动
- **THEN** 配置仍然通过 schema 校验，调试工具使用安全的内置默认值或报告能力未启用

#### Scenario: Workspace override selects a debug profile

- **WHEN** 工作区 `.boxteam/workspace.jsonc` 覆盖 `runtime.debug.launch_profiles`
- **THEN** 后续该工作区的调试启动按照合并后的 profile 解析，且不修改其他工作区配置

### Requirement: Debug configuration separates logical intent from runtime endpoints

`default_adapter`、launch profile 的 `adapter` 和 `runtime` SHALL 表达逻辑调试能力；`program`、`working_directory` 和 `args` SHALL 表达启动目标默认值。Agent 工具可以通过 `configurationName` 选择 profile，但不得直接提交 Inspector WebSocket 地址、VS Code session ID、DAP thread/frame ID 或任意 adapter 内部句柄。

#### Scenario: Configuration name resolves to a launch profile

- **WHEN** Agent 提供 `configurationName` 且该名称存在于当前有效配置
- **THEN** 系统使用对应 profile 的 adapter 和启动默认值，并允许工具调用中的目标文件和工作目录覆盖 profile 的空白默认值

#### Scenario: Unknown configuration name fails explicitly

- **WHEN** Agent 提供不存在的 `configurationName`
- **THEN** 启动失败并报告缺失 profile 名称和可诊断的配置来源，不回退到另一个未请求的 profile

### Requirement: Node Inspector uses loopback and dynamic ports by default

Node Inspector 配置 SHALL 默认绑定 `127.0.0.1` 且默认端口为 `0`。端口为 `0` 时系统 SHALL 为每个调试运行时动态分配可用端口；固定端口只有在配置显式指定时才生效。系统不得默认使用 Web 前端端口 8211 作为 Inspector 端口。

#### Scenario: Concurrent debug sessions use isolated dynamic ports

- **WHEN** 同一工作区或不同 session 同时启动多个 Node 调试会话且 `inspector_port` 为 0
- **THEN** 每个会话获得独立的 Inspector 连接，且不会因为固定端口冲突而错误连接到其他会话

#### Scenario: Invalid or unsafe Inspector endpoint is rejected

- **WHEN** 配置的 Inspector host 为空、端口超出合法范围或违反本地调试安全策略
- **THEN** 配置或调试启动失败并报告具体字段错误，不绑定到不受控的默认地址

### Requirement: Python debugpy configuration is reserved without claiming support

配置 SHALL 允许为未来的 debugpy adapter 预留 host 和 port，但在当前 Python adapter 尚未实现时，选择该 adapter SHALL 返回明确的不支持错误，不得伪装成 Node 调试或返回成功状态。

#### Scenario: Node profile remains the supported first adapter

- **WHEN** Agent 使用 Node Inspector profile 启动 JavaScript 调试
- **THEN** 系统使用 Node 调试实现，不要求配置 debugpy

#### Scenario: Debugpy is selected before implementation exists

- **WHEN** Agent 选择 `debugpy` profile 且当前版本未提供 Python 调试实现
- **THEN** 启动失败并明确说明 debugpy adapter 尚未实现

### Requirement: Configuration values have bounded operational behavior

`command_timeout_seconds` SHALL 是正数；调试输出、动作记录、启动参数和断点数量 SHALL 遵守后端定义的上限；配置不得通过无限制数组或任意命令模板绕过这些限制。新增配置为可选字段时 SHALL 保持现有 `config_version` 兼容，不得为了增加可选字段机械升级版本。

#### Scenario: Invalid timeout is rejected by schema or runtime validation

- **WHEN** `command_timeout_seconds` 小于或等于 0
- **THEN** 有效配置构建失败并报告字段路径

#### Scenario: Optional debug settings do not rewrite user files

- **WHEN** 配置初始化发现已有用户配置但其中没有 debug 字段
- **THEN** 系统只使用内置默认值，不重写或覆盖用户配置文件
