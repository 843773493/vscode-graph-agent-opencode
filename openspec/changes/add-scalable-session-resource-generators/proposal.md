## Why

当前会话侧栏会在启动时拉取所有工作区的全部会话，并依赖扁平目录扫描与前端数组过滤；当本地 Gateway 管理几十到上百个工作区、每个工作区积累上千个会话时，加载、检索和定位都会失去可用性。同时，定时生成、事件生成、批量生成等自动化缺少统一的挂载、命名、幂等、续写与回报模型。

## What Changes

- 新增 Gateway 权威的工作区文件夹树，可分层组织本地与远程工作区，并提供稳定节点 ID、排序和持久化。
- 将会话本体迁移为工作区内的真实物理层级目录；目录树是会话位置和父子会话关系的唯一权威来源，稳定 ID 由 manifest 保留，索引只作为可重建缓存。
- 新增工作区会话目录分页子节点、breadcrumb、名称搜索和可重建 revision，前端不再依赖一次性加载全部会话。
- 新增 Gateway 跨工作区轻量目录索引，统一检索工作区、工作区文件夹、会话文件夹、会话和生成器，并返回可定位的 breadcrumb。
- 新增可注册的通用 Session Generator，拆分 Trigger、Placement、Execution、Context Source、Naming Layout 与 Session Strategy。
- 支持 `new_per_run`、`continue_existing` 和 `fork_new_and_report_back` 三种生成会话策略；生成器可由 Gateway 跨工作区管理和挂载，第一版要求会话、live context 与 Agent 执行位于同一目标工作区。
- 新增生成运行账本、幂等键、失败状态、并发策略、输出会话来源和可观察错误。
- Web 会话侧栏改为按需展开、分页加载、全局搜索定位和生成器状态展示；文件夹与会话的低频管理操作收纳到右键菜单，并支持通过剪贴板稳定 ID 完成移动和子会话绑定。
- Web 资源树支持直接拖放组织：工作区拖到工作区时更新 Gateway `parent_workspace_id`，工作区拖到多层虚拟工作区文件夹时更新导航引用；会话与会话文件夹只在所属工作区的物理树内移动，并可通过拖放建立物理父子会话关系。
- **BREAKING**：不再使用 `.boxteam/sessions/{session_id}/` 作为固定会话路径；所有会话访问、创建、移动、删除、导出和迁移必须经稳定 ID 与物理目录解析服务完成。

## Capabilities

### New Capabilities

- `gateway-workspace-organization`: Gateway 工作区文件夹树、稳定引用、排序、持久化与跨工作区 breadcrumb。
- `scalable-session-navigation`: 工作区物理会话目录、物理父子会话树、稳定 ID、迁移、分页、搜索、定位、revision、索引重建和 Web 懒加载树。
- `session-generators`: 可注册会话生成器、触发器、挂载、命名、执行策略、运行账本、幂等与会话来源。

### Modified Capabilities

- `remote-gateway-federation`: 本地 Gateway 的工作区组织、目录索引和生成器可引用远程 Gateway 投影工作区，并在离线或能力缺失时显式阻塞。

## Impact

- Gateway：注册表旁新增工作区导航、生成器与生成运行持久化服务和 `/api/gateway/*` 控制面 API。
- 工作区后端：新增物理会话目录解析/迁移、目录查询/搜索与生成执行 API，扩展 Session DTO 来源信息和会话执行编排。
- Web：会话侧栏数据模型、API、状态、树组件、搜索和错误反馈将被重构。
- 测试：新增 Gateway/工作区单元测试、正式 E2E、真实浏览器审查与大目录规模回归。
- 存储：Gateway 全局数据继续位于 `${BOXTEAM_HOME:-~/.boxteams}/state/gateway/`；会话业务数据位于目标工作区 `.boxteam/sessions/` 下由文件夹与会话节点组成的物理树，旧扁平目录需要显式迁移。
