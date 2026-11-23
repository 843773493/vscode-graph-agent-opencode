# 客户端源码与测试架构

> **决策状态**：采纳  
> **当前实施范围**：共享 Workbench、Web／Electron 容器、Workspace 辅助服务与测试基础设施

## 1. 当前开发范围

完整界面入口是 `src/clients/workbench/`，Web 与 Electron 共用原版 Workbench 外层及 `src/clients/web/` 的 React 会话产品。`electron/` 已实现窗口、生命周期和固定存储宿主，`electron-web/` 是其 renderer 入口；React Native 和移动 Web parity 仍是 TODO。浏览器验证不能代替打包 Electron 的原生验收。

跨进程协议和纯业务模型不得无故绑定浏览器全局对象。Electron 的文件和网络继续走 Web 的后端 URI、虚拟文件系统及 Gateway；不为未来客户端预写原生 IPC、adapter 或第二套产品实现。

## 2. 源码结构

```text
src/
├── clients/
│   ├── shared/
│   │   ├── core/                  # 纯 TypeScript；无 UI/平台运行时依赖
│   │   ├── web-ui/                # 可复用桌面 React DOM 层
│   │   └── mobile-web-ui/         # 可复用移动 React DOM 层
│   ├── workbench/                  # Web／Electron 共用工作台入口与外层布局
│   ├── web/                        # React 会话组件、业务 Hook 与共享状态
│   ├── electron/                   # 窗口、生命周期、固定存储宿主
│   ├── mobile/                     # TODO
│   ├── electron-web/               # Electron renderer 及浏览器 parity 入口
│   └── mobile-web/                 # TODO，移动布局浏览器 parity
├── workspace-services/
│   ├── browser/
│   └── terminal/
└── shared/                         # 跨进程协议、传输、常量
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
    ↓
src/clients/workbench
    ↓
src/clients/electron-web
```

`src/workspace-services/` 可以依赖 `src/shared/`，不能依赖客户端。共享代码只在复用需求已出现时提取；空的预留目录不应产生占位实现。

## 3. 测试证据分层

### Unit

验证单个函数、类、Hook、组件或状态转换。Python 测试位于 `tests/unit/`，TypeScript/React 单元测试与源码共置。

### Contracts

验证跨进程、跨语言稳定协议，位于 `tests/contracts/`：

- `api/`：Gateway、Workspace HTTP、OpenAPI、认证、`request_id` 与领域 SSE 格式；页面连接另核共享实时 WebSocket 控制及字节合同；
- `clients/`：共享类型、生成类型和客户端状态协议；
- `workspace_services/`：Browser、Terminal 等辅助服务公开边界。测试目录使用下划线以保持 Python 可导入；源码仍使用 `src/workspace-services/`。

### Integration

验证生产组件之间的协作、协议与故障边界，位于 `tests/integration/`。可使用真实依赖，也可在明确外部边界使用模型回放、mini MCP、响应故障注入等替身；有无替身不单独决定目录归属。模拟 Electron bridge 或 React Native Web 只证明相应模拟／Web 运行面，不能代表原生平台验收。

常驻事件的网络证据观察 `/api/gateway/realtime` 的真实订阅、响应头、字节、结束与取消帧，复用 `tests/support/workbench/realtime.mjs`；不能等待已删除的 observation HTTP 请求，或把多个逻辑订阅计为多条物理连接。后端 SSE owner、有限请求流和 PTY／DAP 等专用连接分别验证，不能把取消或连接建立当成业务完成。多视图验收须同时保留普通 HTTP 请求可达与原 owner／游标隔离证据；架构边界见[页面实时订阅](../architecture/runtime-distribution.md#页面实时订阅)。

### E2E

E2E 位于 `tests/e2e/`，承载用户主动说明的完整业务流程验收。新增用例在说明中写明公开入口、关键业务步骤和可观察的最终结果，不另建审批或来源账本。客户端场景实际操作产品客户端并经过 Gateway／Workspace；后端系统场景按其公开入口验收，不宣称覆盖浏览器。

模型边界允许真实调用、上游录制重放或手工 SSE。三者共用已有 HTTP transport 回放入口、fixture 和业务驱动；同一文件切换模型参数时仍属于 E2E，不复制真实／回放两套场景。模型模式与验收目标是两个维度，目录不是依赖真实性的证明。

运行产物注明实际模型模式、响应资产来源及被替换边界。实时录制是本次真实调用；上游重放和手工 SSE 可证明产品如何消费该响应，但不证明真实 Provider 的鉴权、连通性和生成能力。缺少已选择模式的凭据、资产、平台运行时或服务时，必须失败、跳过或报告 `UNMET_PREREQUISITE`，不得静默换模式。

现有归属不明确的测试保留原目录与套件入口，后续触及时再按验证目标判断，不批量迁移。unit 与 contracts 也继续保留。Integration 和 E2E 共同复用 `tests/harness/`、`tests/support/` 的资源生命周期、隔离和清理机制，不各造一套装具。

以下准备和观测设施不替代被验收的业务步骤：

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

## 5. 五类客户端运行面的验证面

| 产品客户端 | 当前状态 | 浏览器测试的含义 | 未来真实 E2E |
|---|---|---|---|
| 纯 Web | 正在开发 | 真实客户端运行面 | Node Playwright |
| Electron | TODO | Web 组件 parity/Integration | Playwright Electron，真实 main/preload/renderer |
| Electron renderer Web | TODO | 浏览器 parity/Integration | Playwright Electron，真实 renderer |
| React Native | TODO | 移动 Web parity/Integration | 真实模拟器或设备驱动 |
| Mobile Web | TODO | 移动布局 parity/Integration | 浏览器移动视口；不等同真机 |

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
