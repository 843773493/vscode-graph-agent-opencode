## Purpose

为thread-owned源码调试资源提供可验证、可分层覆盖的Workspace运行模板，统一管理调试adapter、启动profile、Node Inspector和未来debugpy的端口与执行边界；thread活动方案与运行时状态不反写Workspace配置，也不向模型暴露内部句柄。

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

`runtime.debug`是Workspace级默认值/启动模板，不以Session或Thread为新的JSONC覆盖层；每个`(session_id, thread_id)`的活动方案由受检thread目录/归属manifest绑定，方案正文不包含owner。本次只在同Workspace公开fork的明确模式中复制方案正文，目标owner重新验证源码/工作目录/断点路径及有效profile的adapter/runtime，并在必要时映射目标本地方案ID及记录lineage；不复制活动指针、进程/端口/连接。方案正文保留未来跨Workspace可移植格式，但本次没有跨Workspace复制API。配置仍按Workspace inline→用户→用户本地→工作区递归合并，`workspace_dev.jsonc`是完整开发模板而非隐式合并层。新增thread owner不引入`runtime.debug.thread`开关、不改变已有字段取值语义、不因此升级`config_version`；生效中的调试进程不因其它thread启动或Workspace模板热更新被悄悄替换。

#### Scenario: Existing configuration without debug settings remains valid

- **WHEN** 工作区使用没有 `runtime.debug` 的既有有效配置启动
- **THEN** 配置仍然通过 schema 校验，调试工具使用安全的内置默认值或报告能力未启用

#### Scenario: Workspace override selects a debug profile

- **WHEN** 工作区 `.boxteam/workspace.jsonc` 覆盖 `runtime.debug.launch_profiles`
- **THEN** 后续该工作区各thread的新调试启动按合并后的profile解析；已有进程和各thread已保存的活动方案不被静默改写，也不修改其他工作区配置

#### Scenario: 同Workspace方案复制与Workspace模板分离

- **WHEN** 同Workspace公开fork按模式把已保存方案复制到目标thread，且source capture之后Workspace模板可能变化
- **THEN** target在staging冻结自身已生效Workspace模板revision/hash，按该快照解析profile并验证adapter/runtime、源码/工作目录和全部断点路径，发布前复核revision仍相同；任一缺失、漂移或不兼容使整个fork失败，不静默更换profile；方案正文仍无source/target owner字段，活动方案指针、源进程、Inspector端口和运行状态不复制

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

- **WHEN** 同一Session的main/child、同一工作区的不同Session或不同工作区thread同时启动Node调试且`inspector_port`为0
- **THEN** 每个thread获得独立的Inspector连接和动态端口；端口分配、方案选择、状态或动作不能串到其它thread

#### Scenario: 显式固定端口与已有thread冲突

- **WHEN** 两个thread的有效Workspace模板显式指定同一固定Inspector端口且第一thread已占用
- **THEN** 第二thread的启动明确返回端口占用错误且不接管、重启或停止第一thread进程；不会静默改用另一端口

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
