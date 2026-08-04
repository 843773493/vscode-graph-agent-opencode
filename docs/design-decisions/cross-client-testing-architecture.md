# 跨客户端测试架构设计决策

> **决策状态**：采纳  
> **适用范围**：Workspace 后端、Gateway HTTP、浏览器 Web、VS Code、Electron、React Native 及共享客户端模块

---

## 1. 背景

本项目当前已经实现 Workspace Gateway HTTP、浏览器 Web 和 VS Code 扩展。后续还会增加 Electron 桌面客户端和 React Native 移动客户端。所有客户端都以 Gateway HTTP、SSE 和辅助服务代理作为远程能力边界，但各客户端具有不同的运行环境和系统集成方式。

现有测试已经包含 Python 单元测试、前端 Bun 单元测试、后端真实 HTTP E2E、Gateway E2E 和少量 Playwright Web UI E2E，但仍存在以下结构问题：

1. `tests/e2e/` 根目录混合 Agent、会话、存储和运行时测试，直接文件过多。
2. `tests/e2e/ui/`、`tests/e2e/web/` 和 `tests/e2e/browser/` 的名称无法清晰区分产品 Web 客户端、Web 搜索工具和 Browser Manager。
3. Web UI E2E 在各测试文件中分别管理构建、端口、Gateway、工作区、浏览器和产物；继续复制到 Electron、React Native 会形成多套生命周期实现。
4. Gateway HTTP 是所有客户端共享的协议边界，但缺少独立、系统化的契约测试层。

## 2. 决策目标

测试架构必须满足以下目标：

1. 明确区分单元、契约、集成和端到端测试。
2. Gateway 协议只定义一次，由所有客户端共享。
3. Web、Electron、React Native 使用适合自身平台的测试驱动，不强制使用同一种语言操作客户端。
4. 完整运行时的工作区、端口、进程、控制面目录和产物由统一基础设施管理。
5. Python 后端单元测试目录镜像 `app/`，禁止继续把业务测试堆在 `tests/unit/` 根目录。
6. 正式测试产物继续严格镜像测试文件路径，并写入 `out/tests/`。

## 3. 测试层级

### 3.1 单元测试

单元测试验证单个函数、类、Hook、组件或状态转换。测试不能依赖真实 Gateway、工作区后端、浏览器、模拟器或外部网络。

- Python 后端测试集中在 `tests/unit/`，目录按生产模块镜像。
- TypeScript/React 测试与源码共置，例如 `src/web/src/**/*.test.tsx`。
- 未来共享客户端 package 的测试与对应 package 源码共置。

### 3.2 契约测试

契约测试验证跨进程、跨语言的稳定协议，而不是完整用户流程。Gateway HTTP 契约至少覆盖：

- OpenAPI schema 和生成类型；
- 本地 Token、设备凭据和 Federation 凭据；
- `request_id` 响应体、响应头和代理透传；
- 成功与失败响应结构；
- Workspace 路由头和激活工作区语义；
- SSE 事件名称、数据 schema、心跳和断线行为；
- Terminal、Browser 等辅助服务代理边界。

契约测试位于 `tests/contract/gateway_http/`。客户端不得通过复制 E2E 场景来替代协议契约测试。

### 3.3 集成测试

集成测试验证多个真实模块组合后的行为，但不启动完整产品客户端。允许在进程或外部系统边界使用 fake、stub 或最小 HTTP 服务。

适合的场景包括：

- `GatewayWorkspaceRegistry`、运行时控制器和持久化文件组合；
- Gateway 路由器与最小 Workspace HTTP stub 组合；
- 客户端 Gateway SDK 与协议级 Gateway stub 组合；
- SSE 客户端的取消、重连、乱序和迟到响应处理；
- Electron 主进程服务与 fake Gateway 组合；
- React Native 网络层与协议桩组合。

如果测试需要完整 Gateway、完整 Workspace 后端、真实图形客户端和用户交互，它属于 E2E，不属于集成测试。

### 3.4 端到端测试

E2E 使用真实进程和真实传输验证完整外部行为：

- 后端 E2E：真实 Workspace 后端 HTTP、Agent、会话和工具链；
- Gateway E2E：真实 Gateway、多工作区、进程生命周期、Federation 和代理；
- 客户端 E2E：真实 Gateway、真实 Workspace 后端和真实客户端；
- 工具 E2E：Browser Manager、Terminal Manager、MCP 和 Web 搜索工具链。

## 4. 目录结构

目标结构如下：

```text
tests/
├── support/                         # 共享测试基础设施，不放测试用例
│   ├── paths.py
│   ├── ports.py
│   ├── workspaces.py
│   ├── processes/
│   └── full_stack/
├── unit/                            # Python 单元测试，镜像 app/ 等 Python 源根
│   ├── agents/
│   ├── api/
│   ├── core/
│   ├── gateway/
│   │   ├── control/
│   │   ├── runtime/
│   │   └── server/
│   ├── prompting/
│   ├── runtime/
│   ├── schemas/
│   ├── services/
│   └── tool_testing/
├── contract/
│   └── gateway_http/
├── integration/
│   ├── backend/
│   ├── gateway/
│   └── clients/
└── e2e/
    ├── backend/
    │   ├── agents/
    │   ├── jobs/
    │   ├── sessions/
    │   └── storage/
    ├── gateway/
    │   ├── http/
    │   ├── workspace_runtime/
    │   ├── federation/
    │   └── docker/
    ├── clients/
    │   ├── web/
    │   ├── electron/
    │   ├── mobile/
    │   └── vscode/
    └── tools/
        ├── browser/
        ├── terminal/
        ├── mcp/
        └── web_search/
```

`configs/`、`src/browser/`、`src/terminal/` 等非 `app/` 源根的 Python 测试可以在 `tests/unit/` 下保留同名独立分区，但不能混入某个无关的 `app/` 模块目录。

## 5. 单元测试镜像规则

Python 单元测试以主要被测生产模块决定路径：

```text
app/agents/...                    -> tests/unit/agents/...
app/api/...                       -> tests/unit/api/...
app/core/...                      -> tests/unit/core/...
app/gateway/control/...           -> tests/unit/gateway/control/...
app/gateway/runtime/...           -> tests/unit/gateway/runtime/...
app/gateway/server/...            -> tests/unit/gateway/server/...
app/prompting/...                 -> tests/unit/prompting/...
app/runtime/...                   -> tests/unit/runtime/...
app/schemas/...                   -> tests/unit/schemas/...
app/services/business/...         -> tests/unit/services/business/...
app/services/infrastructure/...   -> tests/unit/services/infrastructure/...
app/services/mapping/...          -> tests/unit/services/mapping/...
app/services/orchestration/...    -> tests/unit/services/orchestration/...
app/tool_testing/...              -> tests/unit/tool_testing/...
```

一个测试引用多个模块时，以测试标题和主要断言对应的公开行为为准，不按 import 数量机械决定路径。跨越多个生产层且无法确定主要模块的测试应重新判断其是否属于 integration。

## 6. 客户端测试驱动

统一的是测试资源生命周期，不是客户端测试语言。

| 客户端 | 单元测试 | E2E 驱动 | 外层资源编排 |
|---|---|---|---|
| Web React | Bun Test | Node Playwright | pytest |
| Electron | Bun Test | Node Playwright Electron | pytest |
| React Native | Jest 或项目选定的 JS runner | Maestro 或 Detox | pytest 或平台启动脚本 |
| VS Code | Bun/Mocha | `@vscode/test-electron` | Node 或 pytest |
| Gateway HTTP | pytest | `httpx` | pytest |

pytest 可以作为完整 E2E 的外层编排器，负责启动后端资源并调用平台原生驱动；不得为了表面统一而用 Python 重写所有浏览器、Electron 或移动端操作。

## 7. 统一完整运行时

共享测试基础设施提供一个明确拥有资源的 `FullStackRuntime` fixture。它至少管理：

- 当前测试的 `output_root` 和 `artifacts_dir`；
- 独立的 `BOXTEAM_HOME`/Gateway 控制面目录；
- 从只读 `asset/` 复制出的一个或多个 Workspace；
- Gateway、Workspace 后端和可选辅助服务进程；
- 端口块、Gateway URL、本地 Token 和 Workspace ID；
- 浏览器 profile、Electron user-data 或移动设备标识；
- 失败日志收集和确定性的逆序清理。

fixture 必须快速失败并暴露完整错误，不允许在依赖缺失或进程异常时返回虚假的可用状态。

## 8. E2E 覆盖策略

避免把所有业务场景在每个客户端重复一遍：

1. Gateway 契约测试负责共享协议的完整性。
2. 共享客户端核心单元测试负责状态机、请求竞态和错误分类。
3. Gateway E2E 负责多工作区和真实进程生命周期。
4. 每个客户端 E2E 负责关键用户路径和平台特有能力。
5. 少量黄金路径在 Web、Electron、Mobile 三端重复执行，用于验证客户端一致性。

客户端黄金路径至少包括：连接 Gateway、浏览工作区、创建会话、发送消息、恢复断线，以及关闭当前活动工作区后切换默认工作区。

## 9. 产物规范

继续遵循测试文件路径镜像规则：

```text
tests/e2e/clients/web/test_workspace_lifecycle.py
-> out/tests/e2e/clients/web/test_workspace_lifecycle/
   ├── workspace/
   ├── gateway-home/
   ├── client-state/
   └── artifacts/
```

Web 截图、Playwright trace、Electron 日志、移动端录屏和性能 JSON 都必须写入对应 `artifacts/`。二进制产物默认不加入 Git。

## 10. 迁移顺序

1. 先让 `tests/unit/` 按生产模块镜像并清空根目录业务测试。
2. 将共享端口、工作区、进程和产物逻辑逐步移动到 `tests/support/`。
3. 建立 `tests/contract/gateway_http/`。
4. 将 `tests/e2e/ui` 迁移为 `tests/e2e/clients/web`。
5. 将 Browser、Terminal、MCP、Web 搜索迁移到 `tests/e2e/tools/`。
6. 最后按领域下沉 `tests/e2e/` 根目录的后端业务测试。

每个迁移步骤必须保持测试可收集、可独立运行，并让正式产物路径镜像迁移后的测试文件路径；不得同时保留新旧兼容入口。
