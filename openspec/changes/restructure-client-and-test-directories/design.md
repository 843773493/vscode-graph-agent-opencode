## Context

当前 `src/` 以 VS Code 扩展为历史中心，同时包含独立纯 Web 工程、Browser/Terminal 辅助服务和共享协议。测试已开始把带服务替身的用例从 `tests/e2e/` 迁入 `tests/integration/`，但目录命名、前端驱动和严格 E2E 判定尚未形成统一结构。现有工作树还有已完成规划的调试工具组改动，本次迁移必须保留这些功能并只改变其路径归属。

仓库当前只继续开发纯 Web 客户端。Electron、React Native 和新 VS Code 客户端架构只是未来目标；现有 VS Code 扩展、Webview 和 Webview UI 不参与此次实现，也不要求通过其构建或测试来证明本变更完成。

## Goals / Non-Goals

**Goals:**

- 给纯 Web、共享客户端逻辑、辅助服务和跨进程协议建立单向依赖边界。
- 让测试目录直接表达测试证据强度，并让所有带替身的完整流程归入 Integration。
- 建立可扩展的客户端场景、Playwright Web 驱动、运行上下文和套件注册位置。
- 迁移时同步活跃脚本、配置、文档和 `AGENTS.md`，不保留旧路径兼容层。
- 保留当前工作树中的目标程序源码调试功能和测试迁移结果。

**Non-Goals:**

- 不实现 Electron、React Native 或新 VS Code 客户端。
- 不迁移、不重构现存 `src/extension.js`、`src/backend/`、`src/webview/` 与 `src/webview-ui/`。
- 不为了未来客户端提前编写 adapter、bridge 或平台兼容代码。
- 不改变后端 API、Agent 行为、调试工具协议或纯 Web 的产品交互。
- 不把历史计划、历史 memory 文档中的路径当作当前命令逐一改写。

## Decisions

### 1. 使用分阶段源码目标，而不是一次搬完所有客户端

本阶段的可运行源码结构为：

```text
src/
├── clients/
│   ├── AGENTS.md
│   ├── shared/
│   │   ├── AGENTS.md
│   │   ├── core/                 # 纯 TypeScript 模型/状态/用例；按需提取
│   │   │   └── AGENTS.md
│   │   └── web-ui/               # React DOM 复用层；按需提取
│   │       └── AGENTS.md
│   ├── web/                       # 当前唯一开发、构建和验证的客户端
│   ├── electron/                  # TODO：未来独立变更实现
│   │   └── AGENTS.md
│   ├── mobile/                    # TODO：未来独立变更实现
│   │   └── AGENTS.md
│   └── vscode/                    # TODO：未来迁移目标
│       └── AGENTS.md
├── workspace-services/
│   ├── AGENTS.md
│   ├── browser/                   # 原 src/browser
│   └── terminal/                  # 原 src/terminal
├── shared/                        # 跨进程 contracts/transport/constants
├── extension.js                   # 本阶段保留的旧 VS Code 实现
├── backend/                       # 本阶段保留
├── webview/                       # 本阶段保留
├── webview-ui/                    # 本阶段保留
└── test/                          # 本阶段保留的扩展测试
```

选择“只迁移活跃纯 Web 与辅助服务”，是为了让目录调整可被真实构建和测试证明，同时遵守用户要求，不把尚未维护的客户端带入变更。替代方案是立即把整个 VS Code 扩展迁入 `clients/vscode`；这会迫使本次修复扩展清单、资源路径和 Webview 构建，扩大为另一个产品迁移，因此拒绝。

### 2. 明确单向依赖方向

目标依赖方向为：

```text
src/shared
    ↓
src/clients/shared/core
    ↓
src/clients/shared/web-ui
    ↓
src/clients/web
```

- `clients/shared/core` 不得依赖 React、DOM、Node.js、VS Code、Electron 或 React Native。
- `clients/shared/web-ui` 可以依赖 React DOM 和 core，但不得导入具体客户端入口。
- `clients/web` 负责页面组合、浏览器状态、API 适配和纯 Web 入口。
- `workspace-services` 可以依赖 `src/shared`，不得依赖客户端。
- 现存 VS Code 代码在后续专门迁移前不受新依赖规则强制重构。

本次只创建 shared 层的职责边界，不机械提取尚未证明可复用的组件。未来复用发生时直接移动源实现，不建立转发模块。

### 3. 测试目录以证据强度为第一维度

目标结构为：

```text
tests/
├── AGENTS.md
├── clients/
│   ├── AGENTS.md
│   ├── scenarios/                 # 跨客户端场景意图
│   ├── selectors/                 # 稳定语义选择器
│   ├── drivers/
│   │   ├── web-playwright/        # 本阶段实现
│   │   ├── electron-playwright/   # TODO
│   │   ├── vscode/                # TODO
│   │   ├── mobile-web-playwright/ # TODO，仅 parity/integration
│   │   └── mobile-native/         # TODO，未来真实原生 E2E
│   └── capabilities/              # 运行面能力声明
├── contracts/
│   ├── api/
│   ├── clients/
│   └── workspace_services/       # Python 可导入目录名
├── integration/
│   ├── backend/
│   ├── gateway/
│   ├── workspace_services/
│   ├── clients/
│   │   └── web/
│   └── stubs/
├── e2e/
│   ├── system/
│   │   ├── agent/
│   │   ├── gateway/
│   │   └── workspace_services/
│   └── clients/
│       └── web/
├── harness/
│   ├── python/
│   └── js/
├── runner/
│   ├── matrix.jsonc
│   └── run-tests.mjs
├── support/                       # 现有 Python helper，逐步下沉至 harness
└── unit/
```

E2E 的关键链路不得出现替身。浏览器驱动本身、隔离工作区、通过真实 API 预置数据、测试账号和只读网络观测不算替身；替换模型、Gateway 下游、MCP、网页响应、宿主 bridge 或原生运行面则算替身。

选择严格定义而不是“浏览器跑起来就算 E2E”，是为了让 E2E 通过能够证明真实部署链路。现有使用替身但覆盖面较大的测试不会删除，只会迁到 Integration 并保留其价值。

### 4. Web 浏览器自动化使用 Node Playwright，不再用 Python 包装每个脚本

浏览器动作、选择器和 trace 生命周期由 JavaScript ESM 实现。Python 只在需要管理 Python 后端 fixture 时作为外层资源编排器；不再为每个 `.mjs` 场景创建只负责 `subprocess` 的同名 pytest 包装器。测试 runner 维护套件注册和可选矩阵，单个驱动返回结构化结果和产物 manifest。

本阶段不强制一次性改写所有已有 pytest Web E2E；先迁移现有 Node 脚本和薄包装层，保留确实承担后端 fixture 的 Python 测试。

### 5. 测试运行上下文显式拥有资源

`tests/harness/js` 与 `tests/harness/python` 提供运行上下文，至少包含：

- 测试文件对应的 `outputRoot`、`workspaceRoot` 与 `artifactsDir`；
- 独立 `BOXTEAM_HOME` 和控制面目录；
- 按测试运行 ID 与 pytest 节点隔离、位于对应 `out/tests/.../runtime/` 下的临时目录；
- 动态端口分配与真实进程生命周期；
- 失败时的日志、截图、trace 和结构化结果；
- 逆序清理与不可用前置条件的明确状态。

沿用现有 `tests/support` helper，先按新边界整理，不为了形式立刻重写成熟 fixture。runner 不得把 `UNMET_PREREQUISITE` 转换成通过，也不得在真实套件失败时调用 stub 套件。

Python 测试统一使用 pytest `importlib` 导入模式，避免不同领域中同名测试模块互相覆盖。runner 为每次套件运行注入 `BOXTEAM_TEST_RUN_ID`；直接执行 pytest 时由 harness 使用进程 ID，最终与测试节点 ID 一起生成隔离运行目录。根 `tmp_path` 与 `BOXTEAM_HOME` fixture 都使用该目录，不依赖多个 pytest 进程会竞争和清理的共享系统临时目录。

### 6. 活跃文档只描述当前路径，历史材料保留历史语义

根 `AGENTS.md`、`src/AGENTS.md`、新目录 `AGENTS.md`、测试层级说明、当前架构决策和开发命令必须切换到新路径。带日期的历史计划与 memory 记录可以保留旧路径，因为它们描述的是当时状态；若会误导当前执行命令，则加醒目标注而不是重写历史。

所有新增源码目录都创建四段式 `AGENTS.md`。预留客户端目录必须明确“TODO、禁止当前实现、需要独立 OpenSpec”。

## Risks / Trade-offs

- [大量路径移动容易漏改脚本或文档] → 用 `rg` 搜索所有旧活跃路径，运行 Web build、JS 测试收集和 Python 测试收集验证。
- [工作树已有大量调试功能改动，移动时可能丢失未跟踪文件] → 迁移前后对 `git status` 和文件清单做对照，禁止 reset/checkout，按整个目录移动未跟踪源码。
- [保留旧 VS Code 目录使 `src/` 暂时不是最终纯净结构] → 在 `src/AGENTS.md` 明确这是阶段性例外，并为 `clients/vscode` 写后续 TODO，不创建双份实现。
- [过早抽象 shared 层会产生无用接口] → 本次只建立目录和依赖规则；仅把已有明确共享协议留在 `src/shared`，不为未来客户端预写适配器。
- [严格 E2E 分类会减少 E2E 数量] → 保留原测试为 Integration，并在套件矩阵中单独报告真实 E2E 与 Integration，不以数量作为覆盖质量代理。
- [一次移动 Browser/Terminal 可能影响打包资源路径] → 搜索发行清单、dev launcher、测试与 Python资源加载位置，逐项更新并执行相关单元测试。

## Migration Plan

1. 建立新目录及四段式 `AGENTS.md`，更新根级架构说明。
2. 原样迁移 `src/web` 到 `src/clients/web`，同步开发、构建、类型生成和配置路径。
3. 原样迁移 `src/browser`、`src/terminal` 到 `src/workspace-services/`，同步生产入口与测试引用。
4. 建立 `tests/clients`、`tests/contracts`、`tests/harness`、`tests/runner` 边界和当前 Web 驱动；将已有测试按严格规则重新分类和改名。
5. 更新活跃文档、`AGENTS.md`、测试命令与产物映射说明。
6. 运行静态分析、纯 Web 生产构建、测试收集、受影响 unit/integration/E2E，并复查旧路径引用。

回滚以本变更的目录移动反向执行；不保留运行时双路径开关或兼容转发器。
