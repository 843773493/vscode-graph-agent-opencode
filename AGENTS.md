# 通用指引

## 开发标准

### 沟通与交付

1. 使用中文进行沟通；代码注释也应使用中文，专业术语除外。
2. 除非用户明确要求，否则不要编写总结性文档。

### 实现方法

1. 第一次实现功能时，尽量减少使用 `try/except`，专注于核心功能。
2. 对于自己不确定的代码部分，在代码中添加 TODO 注释。
3. 当你或用户要求跳过某个重要实现细节时，在代码中添加 TODO 注释。
4. 当使用面向兼容性的代码时，在其上方添加 TODO 注释。
5. 尽量避免使用 `any`；仅在处理泛型或其他复杂情况时使用。
6. 在适当情况下优先使用第三方库；不要重复造轮子。
7. 类：不要使用原型混入或变异，优先使用继承或组合。
8. 当用户要求你重构时或修改现有功能，清理原始代码并直接实现功能；不要保留兼容层。

### 依赖和配置

1. 不要在代码中硬编码环境变量值。
2. 使用 `bun install` 安装 JS/TS 依赖，使用 `uv sync` 安装 Python 依赖。
3. 如果仓库根目录下存在 `.venv` 目录，Windows 使用 `.venv\Scripts\python.exe`，Linux/macOS 使用 `.venv/bin/python`；优先通过 `uv run` 调用 Python、pytest 等命令，不要使用全局 Python 解释器。
4. 将懒加载的包单独放在 `runtime` 模块中。
5. 解析仓库内路径时，默认以项目根目录为起点，优先基于运行时工作目录 `Path.cwd()` 或显式传入的绝对路径，从根目录向下查找各个文件；不要使用 `parent` / `parents` 这类方式通过文件位置向上推导仓库根目录。
6. LLM provider 默认采用最小配置，只要求模型名称、endpoint、API key 和 provider 处理方式；采样参数、reasoning 参数和输出上限由上游模型采用默认值。
7. 不要根据模型上下文窗口推导或擅自添加 `max_tokens`、`max_output_tokens` 等输出限制；上下文容量与单次输出限制是不同概念。
8. 只有用户明确要求，或官方接口验证为必需参数时，才在配置中增加模型请求覆盖；增加前应先测试省略该参数的真实请求行为，并说明它对长文本、成本和工具调用的影响。

### 执行和质量

1. 每次编写代码文件时，都运行静态分析。
2. 每次修改浏览器 UI（`src/web/`）后，都要执行 `bun --cwd src/web run build`；每次修改 Webview UI（`src/webview-ui/`）后，都要执行 `bun --cwd src/webview-ui run build`，并在结束前确认构建成功。

### 代码组织

1. 如果 `package.json` 中的命令过长，将其移到 `scripts/` 下的 `.mjs` 脚本中。
2. 仓库中的 JavaScript 代码必须始终使用 ESM（ES 模块）通过 `import`/`export`，避免使用 CommonJS。
3. `src/web` 与 `src/webview-ui` 负责页面、交互、状态、API 调用以及少量展示逻辑。
4. `app/` 中除 `app/gateway/` 外的工作区后端模块负责 Agent 业务规则、会话状态和核心计算；`app/gateway/` 只负责工作区路由和代理。
5. **UI 优先开发 `src/web`（浏览器前端 8011，端口以 `scripts/dev.mjs` 的 `frontendPort` 为准），稳定后同步到 `src/webview-ui`（VS Code Webview 5173）。用户说"UI"默认指 `src/web`。**

### 提交和目录规范

1. Git 提交应遵循规范的风格，简明扼要，并按逻辑分组。
2. 每个源码目录下创建的子目录必须包含一个 `AGENTS.md` 文件，文件中包含四个部分：“目录用途”、“可修改内容”、“不可修改内容”和“规范”。

### 故障处理

1. 程序绝不能默默失败。

## 本地代理设计原则

### 核心理念

1. 对于在用户自己电脑上运行的工具：诚实地崩溃远比虚假地显示一切正常要好得多。

### 具体原则

1. 快速失败，而不是优雅降级。
2. 永不默默失败。
3. 永不返回虚假的默认值。
4. 抛出尽可能详细的错误。
5. 立即暴露问题。
6. 永不隐藏错误。

## 项目相关

### 目标

1. 这是一个在用户本地工作区运行的 AI 编程助手，由 FastAPI 工作区后端、Workspace Gateway、浏览器前端和 VS Code 扩展共同提供 IDE 级自主编码体验。

### 本地运行时设计

1. 没有云服务功能；没有优雅降级、高可用性或多租户功能。
2. 故障必须透明：直接抛出详细错误，绝不悄无声息地失败。
3. 友好开发者：问题发生时直接崩溃，以便调试。
4. 本地控制面不依赖数据库、消息队列或云端控制服务；模型 Provider、Web 搜索、SSH 工作区等显式配置的外部能力不属于“零网络依赖”。

### 工作区安全性

1. 全局安装、配置和 Gateway 控制面数据统一存储在 `${BOXTEAM_HOME:-~/.boxteams}/`，禁止新增对旧目录 `~/.boxteam/` 的写入。
2. 工作区业务数据必须存储在独立的 `${workspace_abs_path}/.boxteam/` 目录中；不得把会话、检查点、工具结果或 Agent 日志写入全局目录。
3. Gateway 管理多个工作区的注册表、激活状态、SSH 重连信息和自身日志属于全局控制面数据，不得存放在默认工作区或任意工作区的 `.boxteam/` 中。
4. 同一会话的检查点、LLM 请求日志、Trace、后台任务、上下文历史、变更和工具结果统一聚合到 `${workspace_abs_path}/.boxteam/sessions/{session_id}/`，避免删除、导出或迁移时遗留孤儿数据。

### 架构原则

1. 浏览器前端默认请求同源 `/api`：Vite 将请求转发到 Workspace Gateway，Gateway 再路由到当前激活工作区的 FastAPI 后端；不要假设 `src/web` 直接访问 8010。
2. Workspace Gateway 只负责工作区注册、目标生命周期和透明代理，不实现 Agent 业务逻辑，也不直接读写工作区 `.boxteam/` 业务数据。
3. 工作区后端中，`JobService` 调度 `AgentExecutionService`，`AgentExecutionService` 驱动 `DeepAgent` 执行内置工具。
4. Gateway 自有接口使用 `/api/gateway/*`，工作区业务接口使用 `/api/v1/*`；浏览器访问后者时仍先经过 Gateway。
5. Gateway 与工作区后端都必须安装 `TraceMiddleware`。Gateway 生成或接受本次请求唯一的 `request_id`，自有 API 在响应体和 `X-Request-ID` 响应头中返回它，代理 API 通过 `X-Request-ID` 向工作区后端透传同一值；任何一层不得补造第二个请求 ID。
6. 事件总线通过 SSE 向前端推送实时更新。

### 前端状态管理原则

1. 后端是业务状态的唯一权威来源；Gateway 负责选择目标工作区并透明传输，前端只保存展示态和后端状态镜像。
2. 前端不拥有业务状态的权威来源；所有业务状态更改必须通过后端 API，不能仅修改本地 React 状态伪造成功。
3. 成功时，用后端返回的完整对象完全替换前端状态，而不是部分修补字段。
4. 失败时，主动从后端重新获取数据以确保一致性。
5. 这适用于核心业务状态，如代理切换、会话管理和消息发送。

### 测试与依赖注入分层

1. 测试文件里统一用 pytest fixture 进行依赖注入。
2. 应用代码里统一用 FastAPI Depends 进行依赖注入。

### 测试工作区隔离

1. 仓库正式测试脚本的运行工作区必须写入 `out/tests/<与 tests/ 下测试文件相同的路径（去掉 .py 后缀）>/workspace/`。例如 `tests/e2e/mcp/test_mini_mcp.py` 对应 `out/tests/e2e/mcp/test_mini_mcp/workspace/`。
2. Codex/Agent 为当前开发任务执行的临时 Web UI、浏览器、E2E 探索或 subagent 真实操作不属于仓库正式测试脚本；这类临时操作只能使用当前用户明确允许的默认工作区，或使用 `out/tests/temp/<task_name>/workspace/` 下的临时隔离工作区。
3. 测试需要独立工作区时，从 `asset/` 选择合适的测试工作区复制到上述对应的 `workspace/`，再使用复制后的目录；不要直接修改或注册 `asset/` 中的模板目录。
4. 禁止把本项目根目录注册为测试工作区，也禁止为了测试在项目根目录产生 `.boxteam/`、会话、运行时状态或其他测试数据。
5. 向 subagent 委派临时 Web 或 E2E 操作时，任务说明必须明确指定 `out/tests/temp/<task_name>/` 下的工作区和产物目录，不能让 subagent 自行选择目录，也不能新增项目根目录工作区；让 subagent 运行仓库正式测试脚本时，沿用该脚本在 `out/tests/` 下的正式输出路径。

### 测试产物管理

1. 仓库正式测试脚本产生的工作区、日志和可复查产物，统一放在 `out/tests/<同名测试路径>/`；目录结构必须镜像 `tests/` 下的测试文件路径并去掉 `.py` 后缀。
2. Codex/Agent 在开发过程中自行创建的一次性诊断、截图、录屏、HTML 快照、浏览器下载、Playwright trace、审查报告、临时日志和临时工作区，统一放在 `out/tests/temp/<task_name>/`；其中隔离工作区使用 `workspace/`，其余产物使用 `artifacts/`。
3. **严禁在项目根目录生成或保存任何测试图片、截图、录屏或其他测试文件。** 同样不得把临时产物直接放在 `out/tests/` 顶层、`src/`、`app/`、`asset/` 或 `reference_repo/` 中。
4. `asset/` 是只读测试模板目录，不是测试输出目录。正式测试只能写入自己的 `out/tests/<同名测试路径>/`；Agent 临时任务只能写入 `out/tests/temp/<task_name>/`，二者不得混用。
5. Agent 调用截图、浏览器或审查工具前必须显式设置 `out/tests/temp/<task_name>/artifacts/`；仓库正式测试脚本则必须显式设置自己的 `out/tests/<同名测试路径>/artifacts/`。不得依赖工具默认当前目录。
6. Agent 临时任务结束时必须列出本次生成的产物；纯临时产物应主动删除，用户需要查看的临时产物可以保留在对应的 `out/tests/temp/<task_name>/`。仓库正式测试脚本的输出默认保留，以便复查，不得按 Agent 临时产物规则自动删除。
7. 测试生成的二进制文件默认不得加入 Git。只有用户明确要求将其作为长期测试基线或产品资源时，才允许提交，并应放入语义明确的专用目录而不是 `out/tests/` 运行输出目录。

### 运行时说明

1. 在 JS/TS 环境中使用 `bun`；使用 `bun install` 安装依赖，使用 `bun run dev` 启动本地开发环境。
2. `bun run dev` 会执行 `scripts/dev.mjs`。当前主服务监听关系为：工作区后端 `127.0.0.1:8010`、浏览器前端 `0.0.0.0:8011`、Workspace Gateway `127.0.0.1:8014`；Terminal 和 Browser 辅助服务分别使用 8012/8013 与 8015/8016，默认监听 `0.0.0.0`。
3. `scripts/dev.mjs` 启动前会清理 8010–8016 以及调试端口 8002 的旧监听进程，其中包括 Gateway。需要验证完整 Web 产品时必须通过该脚本统一重启，不要只手动重启 8010 后端而保留旧 Gateway 或旧前端。
4. 需要让启动命令返回但服务继续运行时使用 `bun run scripts/dev.mjs --only-launch`；普通 `bun run dev` 会持续管理整组子进程，任一关键进程退出时会停止其余进程。
5. 验证 Web 可用性不能只检查 8010 健康接口或 8011 HTML。至少应通过 8011 实际请求 `/api/gateway/health`、`/api/gateway/workspaces` 和 `/api/v1/workspace`，确认页面初始化链路、激活工作区以及响应头/响应体 `request_id` 均正确；涉及交互时还应进行真实浏览器测试。
6. 在 Python 环境中使用 `uv`；使用 `uv sync` 安装依赖。仅调试单个工作区后端时可使用 `uv run uvicorn app.main:app --host 127.0.0.1 --port 8010`，但这不代表 Gateway 和 Web 全链路已经启动。
7. 工作区后端 API 文档位于 http://127.0.0.1:8010/api/v1/docs；Gateway API 文档位于 http://127.0.0.1:8014/api/gateway/docs。

### 配置

1. 对所有 JS/TS 相关工具使用 `bun`。
2. 对所有 Python 相关工具使用 `uv`。
3. 用户级配置位于 `${BOXTEAM_HOME:-~/.boxteams}/config/boxteam.jsonc`，配置 schema 与其同目录；使用 `python -m configs.boxteam` 生成或安装配置。
4. 工作区级配置位于 `${workspace_abs_path}/.boxteam/boxteam.jsonc`，其有效配置覆盖用户级配置中的同名项。
5. 工作区后端初始化只能创建当前显式工作区的 `.boxteam/` 数据目录，不得顺带创建用户默认工作区或修改 Gateway 全局状态。

## 代理协作

### 协作方式

1. 本项目在整个过程中由 vibe 编码辅助生成。代理的上下文和智能有限，因此如果遇到任何不符合开发标准的情况，请主动告知用户。

### 环境配置

1. 如果在开发过程中遇到环境配置问题，请优先跳过它们，先实现其他部分，并在最后向用户询问配置；不要随意更改环境设置。

## 其他

### 基于代理反馈由用户手动添加的额外说明

1. 模板示例；在整理 `AGENTS.md` 时请保留此行。
