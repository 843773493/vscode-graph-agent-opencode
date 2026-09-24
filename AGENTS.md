# 通用指引

## 开发标准

1. **彻底根除双轨**
   - 编码时严禁为了兼容旧链路而保留冗余工具、过时接口或引入新旧双套实现。发现双轨必须果断统一合并，杜绝双轨并存堆积技术债务。
   - **执行原则：** 废除或替换旧接口前，必须先全仓检索所有直接与间接调用方及测试用例，完成全量平移后再物理下线，严禁遗留悬空调用与未清理的死代码。

2. **务实至上**
   - 坚持"大道至简"的务实编码风格，代码实现聚焦业务流转、核心算法效率、边界与必要异常处理。
   - 严禁过度抽象、多层无效封装与过度防御式编程，不纠缠本地场景非必要的理论级安全问题。

3. **绝对零回归**
   - 代码修改与重构前后，外部输入输出契约与业务行为必须严格一致，严禁破坏现有业务逻辑。
   - 必须通过完备的自动化测试与手动验证保障零回归。

4. **纵向切片与代码减法（严禁局部打补丁）**
   - **彻底重塑而非外层包胶水：** 严禁为了"求稳"而在旧代码上套用适配层、临时兼容函数或堆砌防御性 `if/else` 打补丁。必须直击核心逻辑，要么不改，改就必须自底向上彻底翻新并移除老旧实现。
   - **纵向单链路推进：** 单次改动只聚焦一条清晰垂直的业务子链路（例如：只重构"扫描元数据提取"这一条垂直链路），自底向上改透、测试跑通、老旧逻辑连根拔起，做原子化提交。
   - **坚定做代码减法：** 编码与重构应以降低圈复杂度、精简冗余为核心准则。优先追求代码净行数减少与链路清晰，严禁引入更绕的调用链、冗余的文件或增加维护负担。

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
2. 每次修改浏览器 UI（`src/clients/web/`）后，都要执行 `bun run --cwd src/clients/web build`。

### 代码组织

1. 如果 `package.json` 中的命令过长，将其移到 `scripts/` 下的 `.mjs` 脚本中。
2. 仓库中的 JavaScript 代码必须始终使用 ESM（ES 模块）通过 `import`/`export`，避免使用 CommonJS。
3. `src/clients/` 按运行面组织客户端：`web` 是浏览器中的桌面布局，`electron` 是 Electron 的 main/preload 原生宿主，`electron-web` 是 Electron renderer 的浏览器可运行 parity 客户端，`mobile` 是 React Native 客户端，`mobile-web` 是移动布局的浏览器 parity 客户端；可复用的纯客户端核心和桌面 DOM 组件分别位于 `src/clients/shared/core`、`src/clients/shared/web-ui`，移动 Web DOM 组件可放入 `src/clients/shared/mobile-web-ui`。
4. `app/` 中除 `app/gateway/` 外的工作区后端模块负责 Agent 业务规则、会话状态和核心计算；`app/gateway/` 只负责工作区路由和代理。
5. **当前已实现的页面功能仍只在 `src/clients/web` 开发（浏览器前端 8011，端口以 `scripts/launch/dev.mjs` 的 `frontendPort` 为准）。** `electron-web` 和 `mobile-web` 只承载对应原生端约 90% 的非原生 UI parity 测试，不能替代 Electron、React Native 真机或模拟器测试；`electron/main`、`electron/preload` 和 `mobile` 的原生实现必须通过各自的 OpenSpec 变更进入。用户未指定客户端时，现阶段“UI”仍指 `src/clients/web`。
6. 编写纯 Web 代码时应保持共享协议和纯业务模型不绑定浏览器全局对象，但不要为尚未实现的客户端预写 adapter、bridge 或兼容层。


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

1. 这是一个在用户本地工作区运行的 AI 编程助手，由 FastAPI 工作区后端、Workspace Gateway 和客户端运行面共同提供 IDE 级自主编码体验。

### 界面术语

1. **主窗口**是标准前端工作台，包含会话区、左侧侧边栏、中心工作区、右侧侧边栏和底部面板。
2. 主窗口各区域的数据归属必须统一：
   - 左侧侧边栏存放 Gateway 层级数据，例如 Gateway、连接和其管理的工作区导航；它是 Gateway 控制面的入口。
   - 底部面板存放当前工作区层级数据，例如终端、工作区输出、端口和自动化；底部面板状态按工作区保存，切换工作区时切换对应状态。
   - 右侧侧边栏存放当前会话层级数据，例如会话文件、更改和会话资源；不得在这里放 Gateway 全局或工作区面板。
   - 扩展窗口的资源归属暂不统一规定；扩展窗口可复用并同时承载不同工作区、服务器或 Gateway 资源。
3. 自动化属于当前工作区的底部面板，不再作为右侧侧边栏选项卡；自动化任务内部可以创建或关联会话，但展示入口和面板状态按工作区组织。
4. 不要使用“主页面 / 子页面”描述这两个窗口；扩展窗口不是主窗口的子页面。
5. 不要把扩展窗口称为“浏览器窗口”；浏览器只是扩展窗口中的一个展示区域。
6. 描述 UI 修改时，优先使用“主窗口的左侧侧边栏”“主窗口的底部面板”“主窗口的右侧侧边栏”“扩展窗口的浏览器区域”和“扩展窗口的……资源区域”等完整表达，并明确资源属于会话、工作区还是 Gateway 全局范围。

### 本地运行时设计

1. 没有云服务功能；没有优雅降级、高可用性或多租户功能。
2. 故障必须透明：直接抛出详细错误，绝不悄无声息地失败。
3. 友好开发者：问题发生时直接崩溃，以便调试。
4. 本地控制面不依赖数据库、消息队列或云端控制服务；模型 Provider、Web 搜索、SSH 工作区等显式配置的外部能力不属于“零网络依赖”。

### 工作区安全性

1. 全局安装、配置和 Gateway 控制面数据统一存储在 `${BOXTEAM_HOME:-~/.boxteams}/`，禁止新增对旧目录 `~/.boxteam/` 的写入。
2. 工作区业务数据必须存储在独立的 `${workspace_abs_path}/.boxteam/` 目录中；不得把会话、检查点、工具结果或 Agent 日志写入全局目录。
3. Gateway 管理多个工作区的注册表、激活状态、SSH 重连信息和自身日志属于全局控制面数据，不得存放在默认工作区或任意工作区的 `.boxteam/` 中。
4. 同一会话的 manifest、检查点、LLM 请求日志、Trace、后台任务、上下文历史、变更和工具结果必须统一聚合在 `${workspace_abs_path}/.boxteam/sessions/` 物理目录树中的同一个会话节点目录；保留的 `children/` 目录只承载物理子会话树，不属于父会话自身的附属数据。
5. `.boxteam/sessions/{session_id}/` 只描述根级会话的物理形态，不是允许业务代码拼接的固定定位方式；会话可以位于文件夹或父会话 `children/` 边界下，必须通过稳定 ID 和统一路径解析器取得绝对路径。
6. 会话与会话文件夹的物理目录名必须分别严格等于 `session_id` 与 `folder_id`，显示名只存入权威目录索引，不得参与路径命名。
7. `${workspace_abs_path}/.boxteam/navigation/session-catalog.sqlite` 是会话位置和父子组织的唯一权威来源；物理树（`sessions/YYYY/MM/DD/{session_id}`）是受权威 SQLite catalog 约束的存储结果，folder 是 SQLite-only 节点、无物理目录。软件操作必须同步更新 catalog 与物理目录；检测到绕过软件修改目录结构时必须明确报错，不得扫描磁盘并静默吸收改动。旧 JSON 索引只作显式一次性迁移输入，生产读写链路不双读、不扫盘重建。
8. `parent_session_id` 必须与权威 SQLite catalog 中最近的祖先会话一致；不得再维护第二套父子关系或把 catalog 降级为可重建缓存。

### 架构原则

1. 浏览器前端默认请求同源 `/api`：Vite 将请求转发到 Workspace Gateway，Gateway 再路由到当前激活工作区的 FastAPI 后端；不要假设 `src/clients/web` 直接访问 8010。
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

1. 仓库正式测试脚本的运行工作区必须写入 `out/tests/<与 tests/ 下测试文件相同的路径（去掉测试文件后缀）>/workspace/`。例如 `tests/integration/workspace_services/mcp/test_mini_mcp.py` 对应 `out/tests/integration/workspace_services/mcp/test_mini_mcp/workspace/`。
2. Codex/Agent 为当前开发任务执行的临时 Web UI、浏览器、E2E 探索或 subagent 真实操作不属于仓库正式测试脚本；这类临时操作只能使用当前用户明确允许的默认工作区，或使用 `out/tests/temp/<task_name>/workspace/` 下的临时隔离工作区。
3. 测试需要独立工作区时，从 `tests/fixtures/workspaces/` 选择合适的完整测试工作区 fixture 复制到上述对应的 `workspace/`，再使用复制后的目录；不要直接修改或注册 fixture 源目录。
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

### 参考仓库

1. `reference_repo/` 下的仓库仅用于代码检索和方案对照，根项目 Git 不得跟踪其中任何文件。
2. 不得运行 `reference_repo/` 下的测试；根目录 `bunfig.toml` 已将其从 Bun 测试发现范围排除。

### 运行时说明

1. 在 JS/TS 环境中使用 `bun`；使用 `bun install` 安装依赖，使用 `bun run dev` 启动本地开发环境。
2. `bun run dev` 会通过 `scripts/launch/dev-systemd.mjs` 创建当前 worktree 专属的 transient user-systemd unit，再由 unit 执行 `scripts/launch/dev.mjs`。源码开发未显式设置 `BOXTEAM_HOME` 时默认使用当前 worktree 的 `out/development-runtime/boxteam-home/`。当前主服务监听关系为：工作区后端 `127.0.0.1:8010`、浏览器前端 `127.0.0.1:8011`、Workspace Gateway `127.0.0.1:8014`；Terminal 和 Browser 辅助服务分别使用 8012/8013 与 8015/8016，默认监听 `127.0.0.1`。需要跨机访问时才通过 `BOXTEAM_DEV_LISTEN_HOST` 显式放开。
3. `scripts/launch/dev.mjs` 启动前会清理 8010–8016 以及调试端口 8002 的旧监听进程，其中包括 Gateway。需要验证完整 Web 产品时必须通过该脚本统一重启，不要只手动重启 8010 后端而保留旧 Gateway 或旧前端。
4. `bun run dev` 在 transient unit 和完整服务就绪后返回，`bun run dev:status` 查看状态，`bun run dev:stop` 停止并回收 unit。只有需要前台调试整组进程时才使用 `bun run dev:foreground`；任一关键进程退出时仍会停止其余进程。
5. 验证 Web 可用性不能只检查 8010 健康接口或 8011 HTML。至少应通过 8011 实际请求 `/api/gateway/health`、`/api/gateway/workspaces` 和 `/api/v1/workspace`，确认页面初始化链路、激活工作区以及响应头/响应体 `request_id` 均正确；涉及交互时还应进行真实浏览器测试。
6. 在 Python 环境中使用 `uv`；使用 `uv sync` 安装依赖。仅调试单个工作区后端时可使用 `uv run uvicorn app.main:app --host 127.0.0.1 --port 8010`，但这不代表 Gateway 和 Web 全链路已经启动。
7. 工作区后端 API 文档位于 http://127.0.0.1:8010/api/v1/docs；Gateway API 文档位于 http://127.0.0.1:8014/api/gateway/docs。

### 配置

1. 对所有 JS/TS 相关工具使用 `bun`。
2. 对所有 Python 相关工具使用 `uv`。
3. Gateway 内置默认配置位于发行包 `configs/gateway_inline.jsonc`，用户覆盖位于 `${BOXTEAM_HOME:-~/.boxteams}/config/gateway.jsonc`；Workspace 内置默认配置位于发行包 `configs/workspace_inline.jsonc`，用户覆盖位于同目录 `workspace.jsonc`；两者 schema 与配置文件同目录；使用 `python -m configs.boxteam` 安装或迁移配置。
4. 工作区级配置位于 `${workspace_abs_path}/.boxteam/workspace.jsonc`，其有效配置覆盖 `workspace_inline.jsonc`、用户级 `workspace.jsonc` 和 `workspace_local.jsonc` 中的同名项；Gateway 不得读取该文件。
5. 工作区后端初始化只能创建当前显式工作区的 `.boxteam/` 数据目录，不得顺带创建用户默认工作区或修改 Gateway 全局状态。

## 代理协作

### 协作方式

1. 本项目在整个过程中由 vibe 编码辅助生成。代理的上下文和智能有限，因此如果遇到任何不符合开发标准的情况，请主动告知用户。
2. **派生 subagent 必须显式指定模型 `newapi-local/deepseek-v4.1-flash`，并显式设置 reasoning effort（该模型只接受 `low`/`high`/`xhigh`/`max` 或 1–100 整数，缺省 effort 会直接 400 失败）。** 不指定模型时会静默回退到默认的 gpt 系列；该系列在本仓库有已知问题，且已实测会在「纯结构搬迁」中夹带语义改动（例如把 `RuntimeError` 静默改成 `TypeError` 并删除原有 `noqa` 说明）。模型在 subagent 创建时固定，`followup_task` 无法更改，因此复用旧 subagent 前必须先确认其会话的 `turn_context.model`；凡未显式指定模型的既有 subagent，一律不要复用。
3. **subagent 声称「纯搬迁 / 无行为变更」时，必须独立核验**：用删除行与新增行的规范化多重集比较（例如对 `git show <commit>` 的 `-`/`+` 代码行做逐行多重集差），逐条判定未逐字保留的行属于重命名、注释改写，还是真实语义改动；不得只凭提交信息与测试通过就接受。被删除的符号还须全仓 `rg`（含 `configs/`、`tools/`、`scripts/`）复核是否真的零引用。

### 环境配置

1. 如果在开发过程中遇到环境配置问题，请优先跳过它们，先实现其他部分，并在最后向用户询问配置；不要随意更改环境设置。

## 其他

### 基于代理反馈由用户手动添加的额外说明

1. 模板示例；在整理 `AGENTS.md` 时请保留此行。
