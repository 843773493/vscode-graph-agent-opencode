## MODIFIED Requirements

### Requirement: 配置来源 URI 必须使用 config kind 且只标识来源文件

配置来源的 URI MUST 使用闭集内的 `config` kind，且只标识**可寻址的来源文件本身**。`layer` MUST 作为独立兄弟字段保留，遵循本 capability 的 requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」，MUST NOT 塞进 URI。

**`inline` 来源有 URI**：它是发行包内真实存在的 JSONC 文件（`configs/workspace_inline.jsonc` / `configs/gateway_inline.jsonc`，经 `resolve_config_resource_source` 的 `is_file()` 校验），有稳定 disk 载体。

**ConfigSource 与 schema 的 config URI 尾段形态**：尾段 MUST 取该来源的**逻辑资源名**，MUST NOT 取原始文件名。尾段 MUST 是**单段**、MUST 落在动态段闭合 charset `[A-Za-z0-9_-]` 内、MUST NOT 含点号——真实文件名 `workspace_inline.jsonc` / `gateway_inline.jsonc` 的点号与扩展名不可原样入 URI（`parse_resource_uri` 对含 `.` 的尾段以 `invalid_character` fail-closed 拒绝，该 charset MUST NOT 放宽、MUST NOT 新增转义后门）。每条 `ConfigSource` 的 URI MUST 指向该记录的逻辑来源文件，并与其 `layer` 兄弟字段保持来源一致（`layer` 仍留在 URI 之外，不进 URI 字符串）；这不是 layer 与尾段的全局一对一映射，同一 `inline` layer 可以分别指向 `workspace_inline` 与 `gateway_inline`。规范形态即 `boxteam://{scope}/{scope_id}/resources/config/{logical_source_name}`，其中 `scope`/`scope_id` 按本 capability 的 scope 闭集 requirement 取值（`inline` 层取 `scope=inline` 与真实 `distribution_id`），逻辑资源名取该层可寻址载体的逻辑名（如 `workspace_inline` / `gateway_inline`）。`ConfigSourcesDTO.schema_path` 等 schema 位置字段表示独立配置文件资源地址，按同一单段 `config` URI 规则定位 `workspace_schema` / `gateway_schema`，不因此创建 `sources[]` 条目或伪造 layer。

**目标 Node 扩展的 Code OSS 配置资源使用两个具名模板**，仍沿现有 `config` grammar，不新增 scope、kind 或动态段 charset：

- 目标默认 profile 的原 settings 文件：`boxteam://{gateway_id}/gateway/{gateway_id}/resources/config/extension-host/{backend_id}/profiles/{profile_id}/settings`。
- 明确多根视图的原 workspace 文件：`boxteam://{gateway_id}/gateway/{gateway_id}/resources/config/extension-host/{backend_id}/views/{workspace_id}/workspace`，其中 `workspace_id` 为原初始化合同的稳定 `workspace.id`，由 Gateway、Backend、明确 view 与有序工作区根集合决定，MUST NOT 使用临时 generation 或客户端身份。

上述具名地址仅标识目标原 `profiles.defaultProfile.settingsResource` 与稳定 `.code-workspace` 文件，不建立业务 `ConfigSource`／layer、hash／保存基线或通用文件入口。唯一构造点 MUST 为原配置 URI owner；初始化 DTO MUST 同时提供具名资源 URI 与目标机器原生 file URI，后者只在已认证宿主的明确映射内使用。authority、Gateway scope ID、Backend、profile／workspace ID MUST 与原 host binding 一致；单根视图 MUST 不提供 workspace 文件资源。

读取与写入 MUST 只接受该代际已登记的两个精确文件；stat／watch MAY 接受各自的直接父地址，以支持原配置文件缺失、首次保存及原父目录监听，但 MUST NOT 允许目录枚举、兄弟文件或任意原生路径访问。变化事件 MUST 只发布两个具名文件的资源 URI。重连或 overflow 只使原配置模型重新核验，不读取正文或推进保存基线；错误和代际撤销明确终止。原 FileService／TextFileService／ConfigurationEditing 的 mtime／etag、dirty 与写入队列语义 MUST 保持，不声明跨客户端原子事务。

没有稳定、可寻址来源文件的配置来源 MAY 不带 URI；`runtime_override` 在没有此类文件时 MAY 以 `uri = null` 表达。此时配置来源身份仍独立于 URI，MUST 遵循 requirement「identity 必须独立于 URI 且不跨 scope 混同」与「默认寻址政策必须以 URI 为默认形式」；`uri = null` 只表示来源元数据没有可寻址文件位置，不授权另存原生路径作为备用资源 locator。MUST NOT 将来源身份塞入 URI 或为它编造新的 `kind` / 地址形态。原生文件访问仍由原 owner 执行，展示与诊断路径不替代 config URI。

#### Scenario: config 资源有 URI

- **WHEN** 系统为一条 `inline` 配置来源构造地址
- **THEN** 它使用 `config` kind 的 URI，`layer` 作为兄弟字段随行，URI 字符串本身不含 layer 取值

#### Scenario: config URI 尾段是逻辑资源名而非原始文件名

- **WHEN** 系统为一条 `inline` 层配置来源构造 URI，其底层真实文件名为 `workspace_inline.jsonc`
- **THEN** 尾段取逻辑资源名（`workspace_inline`，单段、落在 `[A-Za-z0-9_-]` 内、无点号），MUST NOT 取含 `.jsonc` 的原始文件名；含点号的尾段被 grammar 以 `invalid_character` 拒绝，且 charset MUST NOT 被放宽

#### Scenario: 没有可寻址来源文件时不编造 URI

- **WHEN** 系统处理没有稳定、可寻址来源文件的 `runtime_override`
- **THEN** `uri` MAY 为空，来源仍按既有身份字段识别；系统 MUST NOT 为内部 SQLite carrier 或 runtime override 编造 URI

#### Scenario: profile 和多根视图按正式模板寻址

- **WHEN** 已认证 Node 宿主以同一目标 profile 打开一个明确多根 view
- **THEN** 初始化按两个具名模板提供目标原 settings 和稳定 workspace 文件的 URI，核验有序根集合；重连保留原 workspace settings 与注释，单根只提供原 settings 资源

#### Scenario: 精确父地址不能扩大配置读取范围

- **WHEN** 原配置模型监听尚不存在的 settings 文件的直接父地址，随后首次保存文件；或调用方尝试枚举父目录、读取兄弟文件或访问另一代际资源
- **THEN** 精确 stat／watch 和首次保存沿已登记目标文件服务执行，事件映射回具名 URI；越界读取、枚举与已撤销代际明确拒绝，不向其它 profile、view 或当前活动工作区回退
