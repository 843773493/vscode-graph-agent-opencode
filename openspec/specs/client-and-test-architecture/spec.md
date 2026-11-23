# client-and-test-architecture Specification

## Purpose
为当前纯 Web 客户端开发和未来多客户端扩展建立可验证的源码依赖边界，按验证目标组织测试，并区分业务流程验收与模型依赖真实性证据。
## Requirements
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
本变更 SHALL 只迁移、开发和验证纯 Web 客户端。`src/clients/electron/`、`src/clients/electron-web/`、`src/clients/mobile/` 与 `src/clients/mobile-web/` SHALL 作为未来边界保留 TODO 和目录规范；本变更不得要求实现或验证现存非 Web 客户端。

#### Scenario: 遇到未实现的客户端运行面
- **WHEN** 目录迁移发现 Electron 或 React Native 运行面尚未实现
- **THEN** 保留对应目录规范，不复制纯 Web 代码，也不为其添加无调用方的适配层

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

### Requirement: 测试按验证目标归属
仓库 SHALL 保留 `unit`、`contracts`、`integration` 与 `e2e` 目录。Integration 验证生产组件之间的协作、协议与故障边界；新增 E2E SHALL 来自用户主动说明的完整业务流程，并在用例说明中写明公开入口、关键步骤和可观察的最终结果，不另建审批或来源登记系统。目录及 pytest 层级标记 MUST NOT 随真实／回放模型参数切换。

E2E SHALL 允许真实模型、上游录制重放和手工 SSE，复用已有模型 HTTP transport、fixture 与驱动，不为依赖模式复制测试主体或另起模型响应服务。运行证据 SHALL 明确实际模式、响应资产来源及被替换边界；回放不得作为真实 Provider 可用的证明。现有归属不明确的测试 SHALL 暂留原目录，不因本次语义调整批量迁移。

Python 测试分区名称 SHALL 使用可导入的 `workspace_services`；源码目录继续使用 `src/workspace-services`。

#### Scenario: 同一业务流程切换模型模式
- **WHEN** 用户提出的 E2E 流程选择真实调用、上游重放或手工 SSE
- **THEN** 同一测试文件留在 `tests/e2e/` 并保持 E2E 标记，产物区分实际模式与响应来源；实时录制记为真实调用，回放不宣称真实 Provider 已验收

#### Scenario: 验证组件故障边界
- **WHEN** 测试目标是验证组件之间的响应、故障归属或协议协作
- **THEN** 测试位于 `tests/integration/`，可使用真实依赖或显式替身；不因某次调用了真实模型就升级为 E2E

#### Scenario: 完整业务流程验收
- **WHEN** 用户主动说明客户端或后端系统的完整业务流程并要求验收
- **THEN** 新增 E2E 从声明的产品公开入口验证关键步骤及最终结果；客户端流程经过实际客户端、Gateway 与 Workspace，后端系统流程不得宣称已覆盖客户端

#### Scenario: 既有测试归属不明确
- **WHEN** 现有测试无法按新语义明确区分 Integration 或 E2E
- **THEN** 保留原路径、调用方与套件入口，后续触及时再判断，不复制或批量搬迁

### Requirement: 客户端测试场景与平台驱动解耦
跨客户端可复用的场景定义、选择器、驱动接口和能力声明 SHALL 位于 `tests/clients/`；平台测试使用适合自身运行面的原生驱动。当前变更 SHALL 只实现纯 Web Playwright 驱动，其他驱动仅预留 TODO。

#### Scenario: 复用黄金路径
- **WHEN** 一个连接 Gateway、浏览工作区或发送消息的场景未来需要在多个客户端验证
- **THEN** 场景意图位于 `tests/clients/scenarios/`，当前由 Web Playwright 驱动执行，不复制业务步骤定义

#### Scenario: 验证 Electron 或移动端
- **WHEN** 后续开始真实 Electron 或 React Native E2E
- **THEN** 分别使用对应原生运行面驱动，而不是把 Web parity 测试宣称为对应平台 E2E

### Requirement: 测试基础设施与产物可审计
Integration 与 E2E 的共享资源生命周期 SHALL 复用 `tests/harness/` 或 `tests/support/`，不得按层级各造一套。测试矩阵与套件注册 SHALL 位于 `tests/runner/`。正式测试产物 MUST 镜像测试文件路径写入 `out/tests/`；测试不得静默切换已选择的依赖模式。

#### Scenario: 运行正式测试
- **WHEN** 任一正式测试创建工作区、日志、截图、trace 或性能数据
- **THEN** 产物根目录与该测试在 `tests/` 下的路径一致，并使用隔离端口和隔离工作区

#### Scenario: 并行运行不同测试进程
- **WHEN** 多个 pytest 进程或 xdist worker 同时运行正式测试
- **THEN** 临时目录与 `BOXTEAM_HOME` 同时按测试运行和测试节点隔离到各自的 `out/tests/.../runtime/`，不得竞争共享系统临时目录

#### Scenario: 已选依赖模式不可用
- **WHEN** E2E 已选模式所需的模型凭据、回放资产、服务或平台运行时不可用
- **THEN** 测试明确失败、跳过或报告 `UNMET_PREREQUISITE`，不得静默换成其他模式；显式选择回放不要求真实模型密钥，产品自身鉴权仍照常验证

### Requirement: 目录职责由文档和 AGENTS 约束
受影响的源码与测试目录 SHALL 提供匹配当前结构的 `AGENTS.md`，并在根级架构文档中明确当前只开发纯 Web、其他客户端是 TODO。所有新增源码子目录 SHALL 包含“目录用途”“可修改内容”“不可修改内容”和“规范”四部分。

#### Scenario: 代理进入预留客户端目录
- **WHEN** 开发代理读取 Electron、Electron Web、Mobile 或 Mobile Web 预留目录的 `AGENTS.md`
- **THEN** 文档明确禁止在本阶段实现该客户端，并将新增工作指向独立 OpenSpec 变更

#### Scenario: 代理修改纯 Web UI
- **WHEN** 开发代理修改 `src/clients/web/` 中的 UI
- **THEN** 最近的目录说明要求运行该 Web 工程的静态分析和生产构建
