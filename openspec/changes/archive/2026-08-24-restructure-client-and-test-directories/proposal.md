## Why

当前 `src/` 同时混放 VS Code 扩展、纯 Web 客户端和工作区辅助服务，`tests/` 也未严格区分带替身依赖的集成测试与真实链路 E2E。随着 Electron、React Native 等客户端进入规划阶段，若不先固定边界，纯 Web 开发会继续扩大跨端耦合并使测试结论失真。

## What Changes

- 将客户端源码归入 `src/clients/`，把跨客户端业务模型、Web DOM 组件和具体客户端入口分层。
- 将浏览器、终端等工作区辅助进程归入 `src/workspace-services/`，将协议、传输和常量归入 `src/shared/`。
- **BREAKING** 调整现有源码目录和测试目录路径；仓库内导入、脚本、配置和文档必须同步更新，不保留旧路径兼容层。
- 当前只开发和维护纯 Web 客户端；Electron、VS Code 与 React Native 客户端只预留职责边界和 TODO，不迁移、不修改也不验证其现有实现。
- 重组 `tests/` 为共享客户端场景/驱动、契约测试、集成测试、严格 E2E、测试运行器和单元测试；关键链路使用任何 stub、fake、mock 或运行时替代面时必须归类为集成测试。
- 引入面向四类客户端的测试能力矩阵，同时只实现纯 Web 当前需要的 Playwright 驱动和测试迁移。
- 同步根目录和受影响目录的 `AGENTS.md`、架构文档、测试文档与命令说明。

## Capabilities

### New Capabilities

- `client-and-test-architecture`: 规定客户端源码边界、当前纯 Web 开发范围、预留客户端兼容方向，以及 unit/contract/integration/E2E 测试分类和目录约束。

### Modified Capabilities

无。

## Impact

- 影响 `src/`、`tests/`、相关 `package.json`/Python 配置、构建脚本、测试脚本、文档和 `AGENTS.md`。
- 不改变现有后端 API、SSE 协议、Agent 业务行为或纯 Web 产品行为。
- 其他客户端仅作为未来兼容目标被记录，不要求本次改动兼容或维护其当前代码。
