## Purpose

为当前纯 Web 客户端开发和未来多客户端扩展建立可验证的源码依赖边界，并通过严格测试分层保证 E2E 结论代表真实产品链路而非替身场景。

## ADDED Requirements

### Requirement: 客户端源码按运行面与复用层分区
仓库 SHALL 将当前维护的纯 Web 客户端放在 `src/clients/web/`，将客户端共享的纯业务模型与 Web DOM 展示层分别预留在 `src/clients/shared/core/` 和 `src/clients/shared/web-ui/`，并将工作区辅助服务放在 `src/workspace-services/`。跨进程协议、传输和常量 SHALL 位于 `src/shared/`，不得反向依赖具体客户端。

#### Scenario: 开发纯 Web 功能
- **WHEN** 开发者新增纯 Web 页面、交互、展示状态或 API 适配
- **THEN** 代码位于 `src/clients/web/`，且构建和开发命令从该目录运行

#### Scenario: 新增可复用客户端逻辑
- **WHEN** 一段新逻辑不依赖 React DOM、VS Code、Electron、Node.js 或 React Native 运行时
- **THEN** 该逻辑可以进入 `src/clients/shared/core/`，并保持对具体客户端零依赖

#### Scenario: 新增 Web 可复用组件
- **WHEN** 一段 React DOM 组件需要由纯 Web 和未来 Electron renderer 共享
- **THEN** 该组件可以进入 `src/clients/shared/web-ui/`，并且不得导入具体客户端应用入口

### Requirement: 本阶段只维护纯 Web 客户端
本变更 SHALL 只迁移、开发和验证纯 Web 客户端。`src/clients/electron/`、`src/clients/mobile/` 与 `src/clients/vscode/` SHALL 作为未来边界保留 TODO 和目录规范；本变更不得要求修改、迁移或验证现存非 Web 客户端实现。

#### Scenario: 遇到非 Web 客户端代码
- **WHEN** 目录迁移发现现存 VS Code 扩展或 Webview 代码
- **THEN** 该代码保持原路径和行为，本变更仅在架构文档中记录后续迁移 TODO

#### Scenario: 设计共享接口
- **WHEN** 当前纯 Web 代码定义可能被未来客户端复用的协议或状态模型
- **THEN** 设计避免绑定浏览器全局对象，但无需为未实现客户端添加适配代码或兼容层

### Requirement: 源码迁移不保留旧路径兼容层
被本变更实际迁移的目录 SHALL 一次性更新仓库内入口、导入、构建脚本、开发脚本和有效文档，不得保留旧目录转发器、重复源码或双路径构建。

#### Scenario: 完成纯 Web 目录迁移
- **WHEN** `src/web/` 被迁移到 `src/clients/web/`
- **THEN** 活跃脚本和文档只引用新路径，旧目录不再作为可运行入口存在

#### Scenario: 完成辅助服务目录迁移
- **WHEN** Browser 或 Terminal 辅助服务迁移到 `src/workspace-services/`
- **THEN** 其生产入口和测试引用全部解析到新路径，旧路径不包含兼容副本

### Requirement: 测试按证据强度严格分类
仓库 SHALL 使用 `unit`、`contracts`、`integration` 与 `e2e` 四类测试。只要关键链路使用 stub、fake、mock、固定场景响应、替代服务、`page.route().fulfill()`、模拟宿主桥接或以 Web 运行面替代原生运行面，测试 MUST 归入 `tests/integration/`，不得归入 `tests/e2e/`。

Python 测试分区名称 SHALL 使用可导入的 `workspace_services`；源码目录继续使用 `src/workspace-services`。

#### Scenario: 测试使用模型替身
- **WHEN** Agent 流程由 stub/fake 模型返回固定响应
- **THEN** 测试位于 `tests/integration/`，即使其余后端、Gateway 和浏览器都是真实进程

#### Scenario: 浏览器拦截并修改响应
- **WHEN** Playwright 路由拦截请求并 fulfill 或修改产品依赖的响应
- **THEN** 测试位于 `tests/integration/clients/web/`

#### Scenario: 真实完整链路
- **WHEN** 测试从真实客户端操作开始，经过真实 Gateway、真实 Workspace 后端和真实外部依赖完成关键用户路径
- **THEN** 测试可以位于 `tests/e2e/`，且缺少真实前置条件时明确报告未满足条件而非回退到替身

### Requirement: 客户端测试场景与平台驱动解耦
跨客户端可复用的场景定义、选择器、驱动接口和能力声明 SHALL 位于 `tests/clients/`；平台测试使用适合自身运行面的原生驱动。当前变更 SHALL 只实现纯 Web Playwright 驱动，其他驱动仅预留 TODO。

#### Scenario: 复用黄金路径
- **WHEN** 一个连接 Gateway、浏览工作区或发送消息的场景未来需要在多个客户端验证
- **THEN** 场景意图位于 `tests/clients/scenarios/`，当前由 Web Playwright 驱动执行，不复制业务步骤定义

#### Scenario: 验证 Electron 或移动端
- **WHEN** 后续开始真实 Electron 或 React Native E2E
- **THEN** 分别使用对应原生运行面驱动，而不是把 Web parity 测试宣称为对应平台 E2E

### Requirement: 测试基础设施与产物可审计
共享资源生命周期 SHALL 位于 `tests/harness/` 或 `tests/support/`，测试矩阵与套件注册 SHALL 位于 `tests/runner/`。正式测试产物 MUST 镜像测试文件路径写入 `out/tests/`；测试不得静默从真实依赖回退到替身依赖。

#### Scenario: 运行正式测试
- **WHEN** 任一正式测试创建工作区、日志、截图、trace 或性能数据
- **THEN** 产物根目录与该测试在 `tests/` 下的路径一致，并使用隔离端口和隔离工作区

#### Scenario: 并行运行不同测试进程
- **WHEN** 多个 pytest 进程或 xdist worker 同时运行正式测试
- **THEN** 临时目录与 `BOXTEAM_HOME` 同时按测试运行和测试节点隔离到各自的 `out/tests/.../runtime/`，不得竞争共享系统临时目录

#### Scenario: 真实依赖不可用
- **WHEN** E2E 所需真实模型、服务、凭据或平台运行时不可用
- **THEN** 测试明确失败、跳过或报告 `UNMET_PREREQUISITE`，不得改用 stub 后继续报告 E2E 通过

### Requirement: 目录职责由文档和 AGENTS 约束
受影响的源码与测试目录 SHALL 提供匹配当前结构的 `AGENTS.md`，并在根级架构文档中明确当前只开发纯 Web、其他客户端是 TODO。所有新增源码子目录 SHALL 包含“目录用途”“可修改内容”“不可修改内容”和“规范”四部分。

#### Scenario: 代理进入预留客户端目录
- **WHEN** 开发代理读取 Electron、Mobile 或 VS Code 预留目录的 `AGENTS.md`
- **THEN** 文档明确禁止在本阶段实现该客户端，并将新增工作指向独立 OpenSpec 变更

#### Scenario: 代理修改纯 Web UI
- **WHEN** 开发代理修改 `src/clients/web/` 中的 UI
- **THEN** 最近的目录说明要求运行该 Web 工程的静态分析和生产构建
