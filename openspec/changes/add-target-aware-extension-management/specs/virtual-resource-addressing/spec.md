## MODIFIED Requirements

### Requirement: kind 闭集定稿且描述符闭集独立不可混用

URI kind 闭集 MUST 为 `agent-spec` | `skills` | `config` | `session` | `files` | `attachments` | `workspace` | `folder` | `extensions`。`attachments` 只允许 workspace scope，目录根为 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/attachments/{attachment_id}`，其后按同一文件路径段规则访问 child；原件与预览只读，派生区复用原文件 owner 的写入能力。`files` 用于 `workspace` scope 的文件树根 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/files/root`，以及 requirement「原生目录根绑定必须由目标 Gateway 唯一持有」登记的 `gateway` scope 原生目录根；`workspace`/`folder` 沿用现行领域导航模板。config 定位配置逻辑来源，session 定位领域对象或由原 owner 公布的产物引用，二者不得被文件工具自行解释成私有目录。

语法 kind 与语义来源描述符 kind MUST 分开：描述符仍只有 `agent-spec`/`skills`，普通 files 导航与扩展包读取不会创建来源描述符。后续扩展必须先在本 owner 更新模板和能力，不存在通用未知 kind fallback；旧 memory 两点式仍拒绝。

`extensions` 只允许 Gateway scope，正式包根为 `boxteam://{gateway_id}/gateway/{gateway_id}/resources/extensions/{backend_id}/{profile_id}/{publisher}/{name}`；Backend、profile、publisher、name 使用现有动态身份段规则，包内 child 沿统一文件段 UTF-8 编码与 containment 规则。唯一构造器归原资源寻址 owner，客户端不得从原生安装目录拼 grammar，也不得将扩展目录伪装成普通 `files` 根。该地址不含版本或 hash；原安装器返回的精确 `extension_key` 作为独立发布选择字段，不由消费者推导。扩展包只公布 stat／read 能力，不授予普通文件工具写入、目录注册或访问安装域其它文件的能力。

当前安装版的包资源读取 MUST 核目标安装／宿主管理任一权限，由原安装 owner 核精确 profile 成员及发布描述，再交目标 Python 文件服务读取字节；原安装锁／引用保护 MUST 覆盖资格核验到读取期间的版本与回收竞态，不借 Node 包文件接口提供通用读取，也不由 Python 扫描或持久化第二份库存。运行实例的旧版资源 MUST 同时匹配原合法实例的客户端、view、generation、profile 和冻结发布键，持续具备宿主管理权限；关闭或撤权后不得凭旧 URI、旧键或剩余安装权限延续读取。相对资源继承同一绑定，不退回当前版；公开披露前重核原授权及目标路由。

#### Scenario: 工作区文件树有正式根

- **WHEN** 用 files/root 定位某个已明确指定工作区的源码树
- **THEN** owner 以 URI/身份解析到目录根并返回目录 shape、规范 URI 与操作能力，可按公布的 child path 规则导航；解析结果和普通 DTO MUST NOT 返回物理根，实际文件访问只在原生 owner 本地使用 root binding，且不依据当前激活工作区替换 scope_id

#### Scenario: 未登记 kind 或 scope 组合被拒绝

- **WHEN** 使用未登记 kind 或在 user/gateway/inline scope 使用 workspace files 根
- **THEN** owner 显式拒绝，不回退为本地普通路径

#### Scenario: 描述符闭集不因目录导航扩张

- **WHEN** 普通 files 目录列举或搜索返回许多文件
- **THEN** 系统不将 files 加入语义来源描述符闭集，也不逐文件建立来源或长期身份

#### Scenario: 同一包地址按独立发布键读取

- **WHEN** 扩展更新后，已有合法 Web 实例继续请求其冻结发布键，而新实例请求当前安装版本
- **THEN** 两者使用同一规范包地址及不同的独立发布选择，分别得到原版本正文，不在 URI 中加入版本、hash 或临时 generation

#### Scenario: 保留目录不能代替读取资格

- **WHEN** 实例关闭或运行权限撤销后，请求者保留旧包地址与发布键，且磁盘仍有该目录
- **THEN** 原 owner 明确拒绝旧版读取；仅安装管理权限只可读取当前安装版，不获得旧实例资格

### Requirement: 配置来源 URI 必须使用 config kind 且只标识来源文件

配置来源的 URI MUST 使用闭集内的 `config` kind，且只标识**可寻址的来源文件本身**。`layer` MUST 作为独立兄弟字段保留，遵循本 capability 的 requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」，MUST NOT 塞进 URI。

**`inline` 来源有 URI**：它是发行包内真实存在的 JSONC 文件（`configs/workspace_inline.jsonc` / `configs/gateway_inline.jsonc`，经 `resolve_config_resource_source` 的 `is_file()` 校验），有稳定 disk 载体。

**ConfigSource 与 schema 的 config URI 尾段形态**：尾段 MUST 取该来源的**逻辑资源名**，MUST NOT 取原始文件名。尾段 MUST 是**单段**、MUST 落在动态段闭合 charset `[A-Za-z0-9_-]` 内、MUST NOT 含点号——真实文件名 `workspace_inline.jsonc` / `gateway_inline.jsonc` 的点号与扩展名不可原样入 URI（`parse_resource_uri` 对含 `.` 的尾段以 `invalid_character` fail-closed 拒绝，该 charset MUST NOT 放宽、MUST NOT 新增转义后门）。每条 `ConfigSource` 的 URI MUST 指向该记录的逻辑来源文件，并与其 `layer` 兄弟字段保持来源一致（`layer` 仍留在 URI 之外，不进 URI 字符串）；这不是 layer 与尾段的全局一对一映射，同一 `inline` layer 可以分别指向 `workspace_inline` 与 `gateway_inline`。规范形态即 `boxteam://{scope}/{scope_id}/resources/config/{logical_source_name}`，其中 `scope`/`scope_id` 按本 capability 的 scope 闭集 requirement 取值（`inline` 层取 `scope=inline` 与真实 `distribution_id`），逻辑资源名取该层可寻址载体的逻辑名（如 `workspace_inline` / `gateway_inline`）。`ConfigSourcesDTO.schema_path` 等 schema 位置字段表示独立配置文件资源地址，按同一单段 `config` URI 规则定位 `workspace_schema` / `gateway_schema`，不因此创建 `sources[]` 条目或伪造 layer。

**目标安装扩展共用的 Code OSS 配置资源使用两个具名模板**，仍沿现有 `config` grammar，不新增 scope、kind 或动态段 charset：

- 目标默认 profile 的原 settings 文件：`boxteam://{gateway_id}/gateway/{gateway_id}/resources/config/extension-host/{backend_id}/profiles/{profile_id}/settings`。
- 明确多根视图的原 workspace 文件：`boxteam://{gateway_id}/gateway/{gateway_id}/resources/config/extension-host/{backend_id}/views/{workspace_id}/workspace`，其中 `workspace_id` 为原初始化合同的稳定 `workspace.id`，由 Gateway、Backend、明确 view 与有序工作区根集合决定，MUST NOT 使用临时 generation 或客户端身份。

上述具名地址仅标识目标原 `profiles.defaultProfile.settingsResource` 与稳定 `.code-workspace` 文件，不建立业务 `ConfigSource`／layer、hash／保存基线或通用文件入口。唯一构造点 MUST 为原配置 URI owner；Node 初始化 DTO MUST 同时提供具名资源 URI 与目标机器原生 file URI，后者只在已认证宿主的明确映射内使用；browser 入口与目标设置编辑器共用具名资源 URI，不将原生地址用于本机文件访问。authority、Gateway scope ID、Backend、profile／workspace ID MUST 与原 owner 授权的目标和明确 view 一致，Node 请求继续匹配原 host binding；单根视图 MUST 不提供 workspace 文件资源。

读取与写入 MUST 只接受已认证目标 profile／明确 view 对应的两个精确文件；Node 请求继续核验原代际，目标安装的 Web 扩展和设置编辑器沿既有用户认证与配置授权访问同一 owner，MUST NOT 为读取目标设置先启动 Node host，也不新增另一配置保存链；stat／watch MAY 接受各自的直接父地址，以支持原配置文件缺失、首次保存及原父目录监听，但 MUST NOT 允许目录枚举、兄弟文件或任意原生路径访问。变化事件 MUST 只发布两个具名文件的资源 URI。重连或 overflow 只使原配置模型重新核验，不读取正文或推进保存基线；错误和代际撤销明确终止。原 FileService／TextFileService／ConfigurationEditing 的 mtime／etag、dirty 与写入队列语义 MUST 保持，不声明跨客户端原子事务。

上述目标配置文件的 stat／read／write、稳定 view 文件物化与外部变化通知 MUST 复用目标侧原 Python 文件／异步监听服务；原 Code OSS 配置模型和编辑语义保持，profile 定位取原安装 owner 的必要描述。常驻 Node 安装器 MUST NOT 对外提供这些通用文件能力，也不为普通设置请求启动 Node host 或新增监听进程。职责迁移 MUST 保持原 URI、安装域／profile 身份和文件归属，不另建寻址登记、复制配置正文或因单个 Backend 重启更换资源身份。

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

#### Scenario: Web 目标设置不依赖 Node 宿主启动

- **WHEN** 用户从 A 页面或直接从 B 页面，为 B 安装的 browser 扩展读取或保存已授权目标 profile 的设置，B 尚无 Node 宿主
- **THEN** 两个入口沿同一配置 owner 和具名 URI 操作 B 的精确文件，Node 宿主未启动；非法目标／view 或权限不足明确拒绝，不退回 A 的 profile，不要求创建虚假的 host generation

#### Scenario: 精确父地址不能扩大配置读取范围

- **WHEN** 原配置模型监听尚不存在的 settings 文件的直接父地址，随后首次保存文件；或调用方尝试枚举父目录、读取兄弟文件或访问另一代际资源
- **THEN** 精确 stat／watch 和首次保存沿已登记目标文件服务执行，事件映射回具名 URI；越界读取、枚举与已撤销代际明确拒绝，不向其它 profile、view 或当前活动工作区回退
