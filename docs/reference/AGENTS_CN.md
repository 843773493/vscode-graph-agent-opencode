# AGENTS.MD（中文翻译）

Telegraph 风格。仅包含根规则。进行子树工作前请先阅读对应目录下的 `AGENTS.md`。

## 开始

- 仓库：`https://github.com/openclaw/openclaw`
- 回复：仅限仓库根目录的引用，如 `extensions/telegram/src/index.ts:80`。不要使用绝对路径或 `~/`。
- 优先运行文档列表：如有 `pnpm docs:list` 请先运行，仅阅读相关文档。
- 修复/分类时只给出高置信度答案：验证来源、测试、已发布/当前行为以及依赖约定后再做决定。
- 依赖决定行为：先阅读上游依赖的文档/源码/类型，不要假设 API、默认值、错误、时序或运行时行为。
- 尽可能现场验证；检查环境变量或 `~/.profile` 中的密钥，不要假设本地测试被阻塞，敏感信息请脱敏。
- 缺少依赖：运行 `pnpm install`，重试一次，然后报告首个可操作的错误。
- CODEOWNERS：日常维护、重构、测试可直接处理；涉及较大行为、产品或安全/所有权变更需征求所有者/评审。
- 用词：产品/文档/更新日志中使用 "plugin/plugins"；`extensions/` 是内部术语。
- 新增频道/插件/应用/文档页面：同步更新 `.github/labeler.yml` 与 GitHub 标签。
- 新增 `AGENTS.md`：请同时添加对应的 `CLAUDE.md` 符号链接。

## 地图

- 核心 TS：`src/`、`ui/``、`packages/`；插件：`extensions/`；SDK：`src/plugin-sdk/*`；频道：`src/channels/*`；加载器：`src/plugins/*`；协议：`src/gateway/protocol/*`；文档应用：`docs/`、`apps/`、`Swabble/`。
- 安装器：与同级 `../openclaw.ai` 一起维护。
- 范围化指南存在于：`extensions/`、`src/{plugin-sdk,channels,plugins,gateway,gateway/protocol,agents}/`、`test/helpers*/`、`docs/`、`ui/`、`scripts/`。

## 架构

- 核心保持与扩展无关。不要在核心内硬编码扩展 ID；当清单/注册表/能力契约可用时，不要依赖它们。
- 扩展仅通过 `openclaw/plugin-sdk/*`、清单元数据、注入的运行时助手、文档化的桶（`api.ts`、`runtime-api.ts`）与核心交互。
- 扩展生产代码不得引入核心 `src/**`、`src/plugin-sdk-internal/**`、其他扩展的 `src/**`，或包外相对路径。
- 核心/测试不得依赖深层插件内部（`extensions/*/src/**`、`onboard.js`）。请使用 `api.ts`、SDK 外观或通用契约。
- 扩展拥有的行为应由扩展自行维护：修复、检测、引导、认证/提供方默认值与工具/设置。
- 所有权边界：仅在所有者模块中修复所有者特定的行为。共享/核心仅提供通用扩展点；不要硬编码所有者 ID、依赖字符串、默认值、迁移或恢复策略。若 Bug 命名了某个扩展或其依赖，先在该扩展中处理，仅当多个所有者需要时才在核心添加通用扩展点。
- 旧配置修复：通过 doctor/修复路径完成，而非在启动/加载时由核心做迁移。
- 核心测试断言扩展特定行为：移至对应扩展或通用契约测试。
- 新增扩展点：保持向后兼容、文档化、版本化；注意第三方插件的存在。
- 频道：`src/channels/**` 是实现；插件作者应使用 SDK 扩展点。
- 提供方：核心拥有通用循环；提供方插件拥有认证/目录/运行时钩子。
- 网关协议变更：优先采用增量方式；不兼容变更需要版本化与文档/客户端跟进。
- 配置契约：导出类型、模式/帮助、元数据、基线、对齐文档。废弃的公开键应保持废弃；兼容性由迁移/doctor 处理。
- 方向：清单优先的控制平面；按需运行时加载器；无隐藏契约绕过；广泛可变注册表是过渡方案。
- 提示缓存：对映射/集合/注册表/插件列表/文件/网络结果使用确定性排序，然后再生成模型/工具载荷；尽可能保留旧会话字节。

## 命令

- 运行时：Node 22+。同时保持 Node 与 Bun 路径可用。
- 安装：`pnpm install`（如触及 Bun 锁/补丁请保持同步）。
- CLI：`pnpm openclaw ...` 或 `pnpm dev`；构建：`pnpm build`。
- 智能门禁：`pnpm check:changed`；解释 `pnpm changed:lanes --json`；预演 `pnpm check:changed --staged`。
- 稀疏工作区：`pnpm check:changed` 对稀疏工作区安全，可能跳过稀疏缺失项目的类型检查；不要为了满足类型检查而扩展稀疏检出。直接 `pnpm tsgo*` 仍为严格模式；需要完整类型证明时使用更完整的工作区。
- 生产检查：`pnpm check`；测试：`pnpm test`、`pnpm test:changed`、`pnpm test:serial`、`pnpm test:coverage`。
- 扩展测试：`pnpm test:extensions`、`pnpm test extensions`、`pnpm test extensions/<id>`。
- 定向测试：`pnpm test <path-or-filter> [vitest 参数...]`；不要直接使用 `vitest`。
- Vitest 标志仅限；不要使用 Jest 标志如 `--runInBand`。串行运行请用 `pnpm test:serial` 或 `OPENCLAW_VITEST_MAX_WORKERS=1 pnpm test ...`。
- 类型检查：仅限 `tsgo` 通道（`pnpm tsgo*`、`pnpm check:test-types`）；不要添加 `tsc --noEmit`、`typecheck`、`check:types`。
- 格式化：使用 `oxfmt`，而非 Prettier。优先 `pnpm format:check` / `pnpm format`；对特定文件可使用 `pnpm exec oxfmt --check --threads=1 <files...>` 或 `pnpm exec oxfmt --write --threads=1 <files...>`。
- 代码检查：使用仓库封装命令（`pnpm lint:*`、`scripts/run-oxlint.mjs`）；不要直接调用通用 JS 格式化/检查工具，除非仓库脚本使用它们。
- 重量级检查：`OPENCLAW_LOCAL_CHECK=1`，模式 `OPENCLAW_LOCAL_CHECK_MODE=throttled|full`；CI/共享使用 `OPENCLAW_LOCAL_CHECK=0`。
- Blacksmith/Testbox：在维护者机器上启用 Blacksmith 时，默认使用 Testbox 进行广泛/共享验证。这包括 `pnpm check`、`pnpm check:changed`、`pnpm test`、`pnpm test:changed`、Docker/E2E/在线/包/构建门禁。不要在本地启动这些广泛门禁，除非用户明确请求本地证明或设置 `OPENCLAW_LOCAL_CHECK_MODE=throttled|full`。
- 本地验证：仅限定向编辑循环，如 `pnpm test <特定文件>`、定向格式化检查、小范围代码检查。若本地命令范围扩大，立即停止并移至 Testbox。
- Testbox 使用：从仓库根目录运行，提前用 `blacksmith testbox warmup ci-check-testbox.yml --ref main --idle-timeout 90` 预热，并复用返回的 `tbx_...` ID 执行所有 `run`/`download` 命令。超时阈值：默认 `90` 分钟；多小时 `240`；全天 `720`；通宵 `1440`；超过 `1440` 需显式批准并清理。
- Testbox 全套件配置：`blacksmith testbox run --id <ID> "env NODE_OPTIONS=--max-old-space-size=4096 OPENCLAW_TEST_PROJECTS_PARALLEL=6 OPENCLAW_VITEST_MAX_WORKERS=1 pnpm test"`。如需安装包验证，优先使用 GitHub `Package Acceptance` 工作流而非临时 Testbox 命令。

## GitHub / CI

- 分类：先列清单，仅水合少数。使用带限制的 `gh --json --jq`；避免重复全量评论扫描。
- 自动 PR/Issue 发现：跳过维护者项目，除非直接相关。不要评论、关闭、标签、重命名、变基、修复或落地，除非 Peter 要求。
- PR 扫描/分类：不主动发表 PR 评论/评审。仅在聊天中报告，或在需要关闭/重复说明时添加理由评论。
- 搜索/去重：优先 `gh search issues 'repo:openclaw/openclaw is:open <terms>' --json number,title,state,updatedAt --limit 20`。
- GitHub 搜索布尔文本较敏感。若 `OR` 查询返回空，拆分关键词并分别搜索标题/正文/评论再下结论。
- PR 简表：`gh pr list ...`；随后 `gh pr view <n> --json number,title,body,closingIssuesReferences,files,statusCheckRollup,reviewDecision`。
- 落地 PR 后：搜索重复的开放 Issue/PR。关闭前请说明原因并提供规范链接。
- GitHub 评论含反引号、`$`、Shell 片段：避免内联双引号 `--body`；使用单引号或 `--body-file`。
- PR 执行产物/截图：附加到 PR、评论或外部制品库。不要将 `.github/pr-assets` 或其他 PR 专属资产加入仓库。
- PR 评审答复必须明确涵盖：我们要修复的行为/问题；相关 PR/Issue URL 及受影响端点/表面；是否为最佳修复，并附代码、测试、CI、当前/已发布行为的高确定性证据。
- CI 轮询：精确 SHA，仅必要字段。示例：`gh api repos/<owner>/<repo>/actions/runs/<id> --jq '{status,conclusion,head_sha,updated_at,name,path}'`。
- 落地后等待：最小化。仅精确落地 SHA。若在 `main` 上被替代，同分支 `cancel-in-progress` 取消是预期行为；一旦本地触及表面有证明即可停止。不要为等待更新的无关 `main` 而等待，除非被要求。
- 等待矩阵：
  - 从不：`Auto response`、`Labeler`、`Docs Sync Publish Repo`、`Docs Agent`、`Test Performance Agent`、`Stale`。
  - 条件性：`CI` 仅精确 SHA；`Docs` 仅文档任务/无本地文档证明；`Workflow Sanity` 仅工作流/组合/CI 策略编辑；`Plugin NPM Release` 仅插件包/发布元数据。
  - 仅发布/手动：`Docker Release`、`OpenClaw NPM Release`、`macOS Release`、`OpenClaw Release Checks`、`Cross-OS Release Checks`、`NPM Telegram Beta E2E`。
  - 显式/表面性：`QA-Lab - All Lanes`、`Scheduled Live And E2E`、`Install Smoke`、`CodeQL`、`Sandbox Common Smoke`、`Parity gate`、`Blacksmith Testbox`、`Control UI Locale Refresh`。
- `/landpr`：不要在 `auto-response` 或 `check-docs` 上等待。除 `check-docs` 已失败并给出可操作错误外，将文档视为本地证明。
- 轮询间隔：30–60 秒。仅在失败/完成或确实需要时拉取作业/日志/制品。
- 落地后清理：`main` 上最少验证可行栏：`pnpm check` + `pnpm test`。
- 硬构建门禁：如构建输出、打包、懒加载/模块边界或发布表面可能变更，则落地前需 `pnpm build`。
- 不要落地无关的格式/代码检查/类型/构建/测试失败。若在最新 `origin/main` 上不相关，请给出范围化证明。
- 生成/API 漂移：`pnpm check:architecture`、`pnpm config:docs:gen/check`、`pnpm plugin-sdk:api:gen/check`。跟踪 `docs/.generated/*.sha256`；完整 JSON 被忽略。

## 代码

- TS ESM，严格模式。避免 `any`；优先真实类型、`unknown`、窄化适配器。
- 禁用 `@ts-nocheck`。仅对必要且有说明的 lint 抑制。
- 外部边界：优先 `zod` 或现有模式助手。
- 运行时分支：使用可辨识联合/封闭代码，而非自由字符串。
- 避免语义哨兵：`?? 0`、空对象/字符串等。
- 动态导入：禁止对同一生产模块混用静态+动态导入。使用 `*.runtime.ts` 懒边界。编辑后运行 `pnpm build`；检查 `[INEFFECTIVE_DYNAMIC_IMPORT]`。
- 循环：保持 `pnpm check:import-cycles` + 架构/依赖图绿色。
- 类：禁止原型混入/变异。优先继承/组合。测试优先使用 per-instance 桩。
- 注释：简明，仅用于非显而易见的逻辑。
- 文件拆分：当清晰度/可测试性提升时，在约 700 行左右拆分。
- 命名：**OpenClaw** 用于产品/文档；`openclaw` 用于 CLI/包/路径/配置。
- 英语：美式拼写。

## 测试

- Vitest。并置 `*.test.ts`；E2E 用 `*.e2e.test.ts`；示例模型 `sonnet-4.6`、`gpt-5.4`。
- 避免依赖 grep 工作流/文档字符串来判定运营商策略的脆弱测试。优先可执行行为、解析配置/模式检查或在线运行证明；将发布/CI 策略提醒放在 AGENTS/docs。
- 清理定时器/环境/全局/模拟/套接字/临时目录/模块状态；`--isolate=false` 安全。
- 热测试：避免每次测试 `vi.resetModules()` + 重量级导入。用 `pnpm test:perf:imports <file>` / `pnpm test:perf:hotspots --limit N` 衡量。
- 扩展深度：纯助手/契约单元测试；每个边界一个集成冒烟。
- 模拟昂贵边界：扫描器、清单、注册表、提供方 SDK、网络/进程启动。
- 优先注入；如需模块模拟，模拟窄的 `*.runtime.ts`，而非宽桶或 `openclaw/plugin-sdk/*`。
- 共享夹具/构造器；删除重复断言；断言可在此回归的行为。
- 不要编辑基线/清单/忽略/快照/预期失败文件以静默检查，除非获明确批准。
- 不要并发运行多个独立 `pnpm test`/Vitest 命令。它们可能竞争 `node_modules/.experimental-vitest-cache` 并因 `ENOTEMPTY` 失败。使用单一分组 `pnpm test ...` 调用，或运行定向通道，或为真正并行 Vitest 进程设置不同的 `OPENCLAW_VITEST_FS_MODULE_CACHE_PATH`。
- 测试工作进程上限 16。内存压力：`OPENCLAW_VITEST_MAX_WORKERS=1 pnpm test`。
- 在线：`OPENCLAW_LIVE_TEST=1 pnpm test:live`；详细 `OPENCLAW_LIVE_TEST_QUIET=0`。
- 指南：`docs/help/testing.md`。

## 文档 / 更新日志

- 文档与行为/API 一起变更。使用文档列表/read_when 提示；文档链接遵循 `docs/AGENTS.md`。
- 更新日志仅面向用户；纯测试/内部通常不记录。
- 更新日志位置：活跃版本 `### Changes`/`### Fixes`；每个新增条目必须至少包含一个 `Thanks @author` 标注，使用 GitHub 用户名。禁止添加 `Thanks @codex`、`Thanks @openclaw`、`Thanks @steipete`。
- 更新日志条目始终单行。禁止换行/续行，以便去重、PR 引用与致谢审计工具正常工作，并保持视觉统一。

## Git

- 通过 `scripts/committer "<msg>" <file...>` 提交；仅暂存目标文件。它会格式化暂存文件；仍需通过门禁。
- 提交：常规风格、简洁、分组。
- 不要手动 stash/自动 stash，除非显式要求。除非显式要求，不要进行分支/工作树更改。
- `main`：禁止合并提交；落地前 rebase 到最新 `origin/main`。在一次性绿色运行并通过简洁 rebase 检查后，不要为了追逐 `main` 而重复运行完整门禁。
- 用户说 `commit`：仅你的更改。`commit all`：所有更改分组提交。`push`：可先 `git pull --rebase`。
- 不要删除/重命名意外文件；如阻塞请询问，否则忽略。
- 批量关闭/重新打开 >5：请带数量/范围询问。
- PR/Issue 工作流：`$openclaw-pr-maintainer`。`/landpr`：`~/.codex/prompts/landpr.md`。

## 安全 / 发布

- 切勿提交真实手机号、视频、凭据、线上配置。
- 密钥：渠道/提供方凭据放在 `~/.openclaw/credentials/`；模型授权配置在 `~/.openclaw/agents/<agentId>/agent/auth-profiles.json`。
- 环境密钥：检查 `~/.profile`。
- 依赖补丁/覆盖/供应商变更需要显式批准。`pnpm.patchedDependencies` 仅限精确版本。
- Carbon 钉住仅限所有者：不要更改 `@buape/carbon`，除非 Shadow（`@thewilloftheshadow`，由 `gh` 验证）要求。
- 发布/发布/版本提升需要显式批准。发布文档：`docs/reference/RELEASING.md`；使用 `$openclaw-release-maintainer`。
- GHSA/公告：`$openclaw-ghsa-maintainer`。
- Beta 标签/版本匹配：`vYYYY.M.D-beta.N` → npm `YYYY.M.D-beta.N --tag beta`。

## 应用 / 平台

- 模拟器/仿真器测试前，请检查真实 iOS/Android 设备。
- “重启 iOS/Android 应用” = 重建/重装/重启动，而非终止/启动。
- SwiftUI：使用 `@Observable`、`@Bindable` 等 Observation 机制，而非新建 `ObservableObject`。
- Mac 网关：使用应用或 `openclaw gateway restart/status --deep`；不要临时使用 tmux 网关。日志：`./scripts/clawlog.sh`。
- 版本更新涉及：`package.json`、`apps/android/app/build.gradle.kts`、`apps/ios/version.json` + `pnpm ios:version:sync`、macOS `Info.plist`、`docs/install/updating.md`。Appcast 仅限 Sparkle 发布。
- 移动 LAN 配对：明文 `ws://` 仅限回环。私有网络 `ws://` 需 `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1`；Tailscale/公网请用 `wss://` 或隧道。
- A2UI 哈希 `src/canvas-host/a2ui/.bundle.hash`：自动生成；忽略，除非运行 `pnpm canvas:a2ui:bundle`；单独提交。

## 运维 / 陷阱

- 远程安装文档：`docs/install/{exe-dev,fly,hetzner}.md`。Parallels 冒烟：`$openclaw-parallels-smoke`；Discord 往返：`parallels-discord-roundtrip`。
- 永远不要编辑 `node_modules`。
- 本地专属 `.agents` 忽略项：`.git/info/exclude`，而非仓库 `.gitignore`。
- CLI 进度：`src/cli/progress.ts`；状态表格：`src/terminal/table.ts`。
- 连接/提供方新增：更新所有 UI 表面、文档、状态/配置表单。
- 提供方工具模式：优先扁平字符串枚举助手，而非 `Type.Union([Type.Literal(...)])`；部分提供方拒绝 `anyOf`。这不是仓库级协议/模式禁令。
- 外部消息：禁止令牌增量频道消息。遵循 `docs/concepts/streaming.md`；预览/阻塞流使用编辑/块并保证最终/回退交付。
