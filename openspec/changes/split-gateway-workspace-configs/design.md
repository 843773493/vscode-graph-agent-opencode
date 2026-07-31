## Context

当前 `configs/config.jsonc` 同时描述 Gateway 与 Workspace，`defaults.py` 动态构建实际配置，开发启动再叠加 `development.overlay.jsonc`。Gateway 因此复用了工作区 `ConfigService`，并可能把默认工作区作为配置来源。安装版、开发版和测试还分别依赖生成器参数，形成多个默认值来源。

现有版本化迁移按单个 JSONC 文件原子升级，但把一个旧文件拆成两个目标文件是跨文件布局迁移，需要额外的恢复边界。Gateway 控制面状态仍属于 `${BOXTEAM_HOME}/state/gateway/`，不能混入配置或工作区数据。

## Goals / Non-Goals

**Goals:**

- Gateway 与 Workspace 使用独立、最小且拒绝越界字段的 schema。
- 默认值、开发差异和发行资源都以仓库中的静态 JSONC 为唯一来源。
- development、源码安装和 npm 安装在运行时使用完全相同的目标路径和加载代码。
- 从现有合并配置可靠迁移到双文件布局，失败时不产生不可恢复的半迁移状态。
- 保持 Workspace 用户配置与显式工作区覆盖的热重载、版本迁移及预检语义。

**Non-Goals:**

- 不把 Gateway 注册表或运行状态移入 JSONC 配置。
- 不让 Gateway 读取或修改任意工作区的 `.boxteam` 业务配置。
- 不长期保留旧文件名、源码 overlay 或 Python 动态默认值的兼容读取。
- 不改变 Agent、LLM、MCP 等 Workspace 配置字段的业务语义。

## Decisions

### Schema 与实例按配置域分开命名

仓库提供 `gateway_config.jsonc`、`workspace_config.jsonc` 两个 schema，以及 `gateway.jsonc`、`workspace.jsonc`、`gateway_dev.jsonc`、`workspace_dev.jsonc` 四个实际配置。安装后 schema 与实例位于 `${BOXTEAM_HOME}/config/`，工作区覆盖固定为 `${workspace}/.boxteam/workspace.jsonc`。

相比继续使用 `boxteam` 总称，这能从文件名直接表达所有权。实际配置统一使用 JSONC，避免模板和持久化文件使用不同解析器。

### 静态模板是默认配置的唯一来源

删除 `defaults.py` 和 development overlay。普通初始化复制两个普通模板；开发安装复制两个完整 dev 模板。CLI、Launcher 和 npm runtime 都调用同一个资源安装器，只通过显式 profile 选择源文件，不在内存中拼装字段。

相比生成器，完整模板存在少量重复，但可以直接审查、被编辑器校验，并消除 Python、overlay 与 JSON Schema 之间的隐式合并行为。测试将验证四个模板分别符合其 schema，并约束 dev/普通模板只在预期字段上不同。

### 两个运行时拥有各自加载器

Gateway 配置加载器只接受 `gateway.jsonc`，验证独立版本及 Gateway 工作区注册声明。Workspace `ConfigService` 只接受用户级 `workspace.jsonc` 和显式工作区的 `.boxteam/workspace.jsonc`，分别迁移、验证后再深度合并。两个 schema 都设置 `additionalProperties: false`，越界字段立即报错。

相比让 Gateway 继续复用 Workspace `ConfigService`，独立加载器能移除默认工作区依赖，并避免 Gateway 初始化 Agent/LLM 配置。

### 新配置域独立从版本 1 开始

`gateway.jsonc` 与 `workspace.jsonc` 分别维护自身的 `config_version`。旧合并文件的 1–4 版本先走现有内容迁移得到当前旧形态，再由布局迁移器拆成两个新域的 v1 文件。以后两个域各自演进，不要求同步升版。

### 布局迁移可恢复且保留旧源直到提交完成

启动器在任何服务启动前执行布局迁移：读取并迁移旧文件到内存，构建两个目标候选，分别通过新 schema 验证，然后写入同目录临时文件和迁移状态文件。目标文件用原子替换提交；进程中断后依据状态文件和内容摘要幂等继续。只有两个目标都验证存在后才移除旧源与状态文件。

如果新旧目标同时存在且无法证明由同一次迁移生成，启动明确失败并要求用户处理，不猜测优先级。工作区级旧文件只迁移 Workspace 字段；出现 `gateway` 字段直接报错。

### 开发资产安装不参与配置生成

`gateway_dev.jsonc` 声明开发容器注册项，默认禁用，避免未 provision Docker 目标时破坏普通开发启动。`gateway_development_assets.py` 只安装 SSH key、专用 known-hosts 和托管 SSH config 块。E2E/provision 流程显式启用隔离 `BOXTEAM_HOME` 中的目标配置。

## Risks / Trade-offs

- [静态普通/开发模板可能漂移] → 两套 schema 校验，并测试 dev 模板与普通模板的允许差异。
- [跨文件迁移在进程中断时只提交一个目标] → 使用状态文件、内容摘要与保留旧源实现幂等恢复。
- [用户已手工创建新文件导致迁移冲突] → 校验目标内容；不能证明一致时快速失败，不覆盖用户文件。
- [文件改名影响大量 fixture 与打包脚本] → 先集中路径 API，再机械更新调用方，覆盖源码开发和搬迁 npm smoke test。
- [独立 Gateway loader 与 Workspace loader 产生重复解析代码] → 复用通用 JSONC 读取、环境变量插值、schema 校验和原子写工具，不复用领域模型。

## Migration Plan

1. 增加双 schema、四模板和模板验证测试。
2. 增加集中配置路径与资源安装 API，并让开发/安装启动写入双文件。
3. 实现 Gateway 专属加载器，收窄 Workspace `ConfigService`。
4. 实现用户级与工作区级布局迁移，使用 fixture 覆盖中断恢复和冲突失败。
5. 更新 npm runtime 资源、E2E fixture、CLI 和文档后删除旧生成器、overlay 与旧路径 fallback。
6. 完整验证开发启动、普通启动和 npm 搬迁安装；确认运行时不访问源码配置。

回滚到旧代码无法读取新布局，因此本变更不提供自动降级写回。迁移在删除旧源前完成全部新文件验证，以便失败时修复或使用旧版本读取尚未提交的旧配置。

## Open Questions

- 无。
