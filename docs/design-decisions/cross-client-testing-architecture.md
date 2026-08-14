# 客户端源码与测试架构

> **决策状态**：采纳  
> **当前实施范围**：纯 Web 客户端、共享边界、Workspace 辅助服务与测试基础设施

## 1. 当前开发范围

当前唯一开发、构建和验证的客户端是 `src/clients/web/`。Electron、React Native 和新的 VS Code 客户端只预留目录与依赖方向，都是 TODO；当前功能不需要同步到这些客户端，也不修改现存 `src/extension.js`、`src/backend/`、`src/webview/` 或 `src/webview-ui/`。

纯 Web 代码仍需考虑未来兼容性：跨进程协议和纯业务模型不得无故绑定浏览器全局对象。但兼容性只约束边界设计，不代表要提前编写 Electron preload、React Native adapter 或 VS Code bridge。

## 2. 源码结构

```text
src/
├── clients/
│   ├── shared/
│   │   ├── core/                  # 纯 TypeScript；无 UI/平台运行时依赖
│   │   └── web-ui/                # 可复用 React DOM 层
│   ├── web/                        # 当前唯一维护的客户端
│   ├── electron/                   # TODO
│   ├── mobile/                     # TODO
│   └── vscode/                     # TODO，现存实现尚未迁移
├── workspace-services/
│   ├── browser/
│   └── terminal/
├── shared/                         # 跨进程协议、传输、常量
├── extension.js                    # 阶段性保留的旧 VS Code 实现
├── backend/
├── webview/
├── webview-ui/
└── test/
```

依赖方向固定为：

```text
src/shared
    ↓
src/clients/shared/core
    ↓
src/clients/shared/web-ui
    ↓
src/clients/web
```

`src/workspace-services/` 可以依赖 `src/shared/`，不能依赖客户端。共享代码只在复用需求已出现时提取；空的预留目录不应产生占位实现。

## 3. 测试证据分层

### Unit

验证单个函数、类、Hook、组件或状态转换。Python 测试位于 `tests/unit/`，TypeScript/React 单元测试与源码共置。

### Contracts

验证跨进程、跨语言稳定协议，位于 `tests/contracts/`：

- `api/`：Gateway、Workspace HTTP、OpenAPI、认证、`request_id` 和 SSE；
- `clients/`：共享类型、生成类型和客户端状态协议；
- `workspace_services/`：Browser、Terminal 等辅助服务公开边界。测试目录使用下划线以保持 Python 可导入；源码仍使用 `src/workspace-services/`。

### Integration

验证多个真实生产模块组合，但允许在明确外部边界使用替身，位于 `tests/integration/`。以下任一条件命中就属于 Integration：

- stub、fake、mock 模型或 Provider；
- mini MCP、替代 Gateway 下游或替代网页服务；
- 固定场景响应；
- `page.route().fulfill()` 或修改产品依赖响应；
- 模拟 Electron bridge、VS Code host；
- React Native Web 替代原生运行面。

浏览器本身是真的、Gateway/后端大部分是真的，均不能抵消关键链路存在替身这一事实。

### E2E

E2E 位于 `tests/e2e/`，必须使用场景要求的真实进程、真实传输和真实外部依赖。缺少真实模型、凭据、平台运行时或服务时，测试必须失败、跳过或报告 `UNMET_PREREQUISITE`；不得切换到 stub 后继续报告 E2E 通过。

以下测试设施不算替身：

- 从 `asset/` 复制到隔离目录的测试工作区；
- 通过真实 API 预置状态；
- 测试账号和显式测试配置；
- 只读网络、控制台和 trace 观测；
- 由测试拥有的动态端口和进程生命周期。

## 4. 测试目录

```text
tests/
├── clients/
│   ├── scenarios/                  # 跨客户端场景意图
│   ├── selectors/                  # 稳定语义选择器
│   ├── drivers/
│   │   ├── web-playwright/         # 当前实现
│   │   ├── electron-playwright/    # TODO
│   │   ├── vscode/                 # TODO
│   │   ├── mobile-web-playwright/  # TODO，只能算 parity/integration
│   │   └── mobile-native/          # TODO，未来原生 E2E
│   └── capabilities/
├── contracts/
│   ├── api/
│   ├── clients/
│   └── workspace_services/
├── integration/
│   ├── backend/
│   ├── gateway/
│   ├── workspace_services/
│   ├── clients/web/
│   └── stubs/
├── e2e/
│   ├── system/
│   │   ├── agent/
│   │   ├── gateway/
│   │   └── workspace_services/
│   └── clients/web/
├── harness/
│   ├── python/
│   └── js/
├── runner/
│   ├── matrix.jsonc
│   └── run-tests.mjs
├── support/                        # 现有 helper，逐步由 harness 收口
└── unit/
```

## 5. 四类客户端的验证面

| 产品客户端 | 当前状态 | 浏览器测试的含义 | 未来真实 E2E |
|---|---|---|---|
| 纯 Web | 正在开发 | 真实客户端运行面 | Node Playwright |
| Electron | TODO | Web 组件 parity/Integration | Playwright Electron，真实 main/preload/renderer |
| VS Code | 旧实现暂留，新增开发 TODO | Webview 预览/Integration | VS Code Extension Host |
| React Native | TODO | RN Web parity/Integration | 真实模拟器或设备驱动 |

统一的是场景意图、资源生命周期、产物格式和结果状态，不是强迫所有平台使用同一种驱动语言。

## 6. 运行与产物

JavaScript 浏览器动作使用 Node Playwright ESM；Python 只在需要管理 Workspace/Gateway fixture 时作为外层编排器。纯粹调用同名 `.mjs` 的 Python 薄包装不保留。

正式测试产物继续镜像测试文件路径：

```text
tests/e2e/clients/web/test_workspace_lifecycle.mjs
-> out/tests/e2e/clients/web/test_workspace_lifecycle/
   ├── workspace/
   ├── gateway-home/
   └── artifacts/
```

运行上下文显式拥有隔离工作区、`BOXTEAM_HOME`、端口、进程、浏览器 profile 和产物目录，并按逆序确定性清理。Python 使用 pytest `importlib` 导入模式；`tmp_path` 与 `BOXTEAM_HOME` 由测试运行 ID 和节点 ID 共同隔离到对应 `out/tests/.../runtime/`，允许独立 pytest 进程和 xdist worker 并发运行。runner 必须分别汇报 passed、failed、skipped 与 `UNMET_PREREQUISITE`，不能隐藏或降级失败。
