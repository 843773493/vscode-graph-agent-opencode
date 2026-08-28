## Purpose

为 Gateway、Workspace Backend、Terminal Manager、Browser Manager 及 Web 客户端建立统一、可版本化的跨进程协议契约，确保协议类型、目录归属和跨语言生成结果具有稳定且可验证的来源。

## ADDED Requirements

### Requirement: Protocol domains SHALL have independent versioned namespaces

协议源 SHALL 为 `common`、`gateway`、`workspace`、`terminal` 和 `browser` 建立独立的 package 与版本边界。公共 package 只能承载跨域稳定的基础类型，不得承载某个服务的业务对象。

#### Scenario: Service-specific protocol changes

- **WHEN** Terminal Manager 新增终端输出控制字段
- **THEN** 该字段只修改 Terminal 协议及其绑定，不要求 Gateway 或 Workspace 业务协议新增同名字段

#### Scenario: Shared protocol changes

- **WHEN** 公共错误或请求上下文协议发生不兼容变更
- **THEN** 系统 SHALL 创建新的公共协议版本，并保留旧版本可供仍未迁移的进程使用

### Requirement: Generated bindings SHALL preserve protocol dependency boundaries

每个协议源文件 SHALL 生成对应的 Python、Node/JavaScript 或 TypeScript 绑定；跨文件类型 SHALL 通过生成的 import 关系引用，不得仅因被其他协议引用而在多个生成文件中复制为独立的同名公共定义。

#### Scenario: Cross-file protocol reference

- **WHEN** SSE 协议引用 Workspace 会话事件协议
- **THEN** 生成的 SSE 绑定 SHALL 引用会话事件绑定，且 SHALL 保持与协议源文件对应的目录层级

#### Scenario: Generated output regeneration

- **WHEN** 删除一个协议消息并重新生成绑定
- **THEN** 被删除消息的 Python、Node/JavaScript、TypeScript 绑定及公共导出 SHALL 同步消失，不能残留只在生成目录中存在的类型

### Requirement: Process boundaries SHALL use protocol adapters

HTTP JSON、SSE、Terminal WebSocket 和 Browser WebSocket 的跨进程数据 SHALL 经过对应协议适配层编码或解码。Workspace 内部事件总线和业务模型 SHALL 不因公开协议迁移而直接依赖 Gateway、Terminal 或 Browser 的传输对象。

#### Scenario: Workspace event to SSE

- **WHEN** Workspace 内部事件总线产生文本增量、任务状态或工具调用事件
- **THEN** SSE 适配层 SHALL 将其转换为 Workspace 协议事件后发送，并对无法映射的事件返回明确错误

#### Scenario: Terminal manager request

- **WHEN** Workspace 通过 Terminal Manager Client 创建、写入、读取或关闭终端
- **THEN** 请求和响应 SHALL 使用 Terminal 协议适配层，Workspace 业务代码 SHALL 不直接拼接 Terminal Manager 的传输 JSON 或 WebSocket 消息

#### Scenario: Browser manager request

- **WHEN** Browser Manager 接收页面操作、输入、帧或下载请求
- **THEN** Browser Manager SHALL 使用 Browser 协议解析消息，并对协议字段错误返回可诊断的错误

### Requirement: Initial migration SHALL preserve existing JSON-facing behavior

迁移首阶段 SHALL 保持当前浏览器前端可观察的 HTTP JSON 和 SSE 字段语义、错误传播和事件顺序兼容。Protobuf 的 oneof、Struct、Timestamp 等内部表达 SHALL 通过适配层转换为现有 JSON 形状，直到对应客户端完成迁移。

#### Scenario: Existing SSE consumer

- **WHEN** 现有 Web 前端订阅 Workspace Job SSE
- **THEN** 前端 SHALL 继续收到现有事件外层结构、事件类型和 payload 字段，不需要因协议源切换而改变业务渲染逻辑

#### Scenario: Dynamic metadata

- **WHEN** 协议包含 metadata、raw payload 或其他动态对象
- **THEN** JSON 适配层 SHALL 保留其 JSON 对象语义，不得静默丢弃未知键或把对象转换成虚假的默认值

### Requirement: Protocol evolution SHALL be validated before publication

协议变更 SHALL 在生成前经过格式、依赖和兼容性检查；字段编号、package 版本和删除字段 SHALL 遵守可演进规则。生成代码 SHALL 被视为构建产物，不得手工修改后作为协议来源。

#### Scenario: Incompatible field change

- **WHEN** 变更复用已发布字段编号或删除仍被旧版本使用的字段
- **THEN** 协议检查 SHALL 失败并阻止生成或发布流程继续

#### Scenario: Generated contract verification

- **WHEN** CI 或本地协议生成命令运行
- **THEN** 系统 SHALL 同时验证协议源、生成绑定、JSON/SSE 适配和代表性跨进程消息样例
