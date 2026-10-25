## Purpose

附件目录修订由 `add-managed-attachment-directories` 实施：本唯一 owner 已登记 workspace `attachments` kind 和目录/文件 child 模板，新上传只发布目录与原件 URI。旧 Session 附件 URI 仅供迁入后的历史身份解析，不再生成；完整验收状态见该 change，不新增另一套 URI parser。

为软件内部与模型可见载荷中的资源引用建立**唯一**的寻址抽象与词汇：严格区分资源身份（ResourceIdentity）、资源 URI与真实路径（real path），把 `workspace`、`user`、`gateway`、`inline` 等作用域与其它工作区、其它 gateway 收敛到同一套寻址，并以顶层 gateway 之间的**星型解析**完成跨边界定位。本 capability 是这套抽象、scope 闭集、scope_id 语义表、URI 语法、kind 闭集与拒绝码登记处的唯一 owner。

**契约版本**：本 capability 采用**契约修正 v2**（保留既有段序，`resources` 为保留固定段，`scope_id` 对**所有** scope 都必填），并已依据**仓库内受版本控制的事实源**（`app/core/resource_uri.py` 与 `app/services/infrastructure/resource_platform/virtual_resources/` 下的 `resolver.py`、`values.py` 三处逐字取值，关键取值已内联在本 spec 各处；不引用任何 `out/tests/temp/**` 临时产物）**定稿** scope 闭集、scope_id 语义、kind 闭集与拒绝码登记。`memory` 已确证**不是 URI scope**，MUST NOT 出现在闭集内。

## ADDED Requirements

### Requirement: 术语必须区分机制、地址、身份与真实路径

系统 MUST 使用以下唯一术语表；其它 change、文档和实现说明 MUST 引用本表，不得另定义同义名称。

| 对象 | 唯一名称 |
|---|---|
| 整套机制 | 统一资源寻址 |
| 具体地址 | 资源 URI，简称 URI |
| 业务层稳定身份 | ResourceIdentity |
| 底层文件位置 | 真实路径 |

主线程 MUST 按本 change 的 tasks 1.1/6.6 清理当前源码、公共契约、文档与引用中的旧名称，MUST NOT 保留同义类型、字段、函数或运行时别名。该收口只调整命名，不改变既有寻址语法、URI 字节、稳定身份、哈希或授权语义；原始历史证据不改写。

#### Scenario: 描述寻址机制与具体资源地址

- **WHEN** 当前文档、接口或代码说明描述整套资源寻址能力及其中一个具体地址
- **THEN** 机制称统一资源寻址、地址称资源 URI 或 URI，并分别保留 ResourceIdentity 与真实路径的原职责

#### Scenario: 名称收口不保留旧别名

- **WHEN** 主线程完成任务 6.6 的全量名称和引用迁移
- **THEN** 当前实现和规范只使用本表名称，直接及间接消费者、公共字段和生成类型同步迁移，且既有 URI 字节与业务行为保持一致

### Requirement: 三层职责必须严格分离

系统 MUST 把资源引用区分为三层，每层有唯一 owner 且职责不可互换：

- **资源身份 / ResourceIdentity**：不透明、稳定、**不含 revision**、**不依赖当前激活工作区**，持久化，用于去重与 lineage。
- **资源 URI**：可解析的地址，持久化，**允许悬空**（指向已不存在的资源是合法值），**禁止编码 revision 或 hash**，是软件内部与模型可见载荷中传递资源的默认形式。
- **真实路径 / real path**：业务资源访问时在机器本地临时使用，只在原生 owner 的最终 fs/sqlite 调用点及其必要的局部调用链中存在；除本 capability「原生目录根绑定必须由目标 Gateway 唯一持有」及「工作区物理根必须绑定到唯一原生 Gateway」定义的控制面根绑定，以及下述「Gateway 原始诊断日志管理入口保留原文」和「Browser 本地文件页面 URL 保留原生语义」具名例外外，MUST NOT 持久化。真实路径 MUST NOT 作为资源引用进入模型可见载荷、普通 API/Workspace 响应 DTO、解析结果或跨 Gateway 资源投影。具名输入边界有两类：该 root-binding requirement 授权的原生目录选择/工作区注册，以及本 capability「普通文件入口必须保留原生路径输入并统一目标解析」登记的普通文件路径输入。输入许可不扩大真实路径输出或持久化例外。

#### Scenario: 业务资源 real path 只在本地访问链中存在

- **WHEN** 某个 owner 需要访问底层文件或 sqlite 资源
- **THEN** 原生 owner 在本地取得 real path 并只在访问调用链内使用，不写入业务持久化记录、普通 API 响应体或模型可见载荷；工作区文件根只从唯一原生 Gateway root binding 取得，注册请求须先通过 `add-multi-workspace-backend-mounting` 的唯一授权合同，解析结果仍只含 URI/身份/内容/操作结果

#### Scenario: real path 外泄即缺陷

- **WHEN** API 响应体、业务持久化记录、解析结果、跨 Gateway 资源投影或系统生成的模型资源引用中出现 real path，且不属于上述控制面根绑定或两个具名入口例外
- **THEN** 系统 MUST 将其判定为缺陷并显式失败，不得以 `display_uri`、日志脱敏或截断静默掩盖；root binding 也 MUST NOT 出现在上述输出中

#### Scenario: 工作区根绑定只在原生控制面持久化

- **WHEN** 系统持久化已注册工作区的物理根
- **THEN** 该根 MUST 只保存在负责原生 target 的 Gateway 控制面 registry，并按 target ID 与 Backend `workspace_id` 绑定；用户提供的注册根只可经身份认证且通过 `add-multi-workspace-backend-mounting` 目标 Gateway 授权的请求送达该 owner。managed group、业务记录、普通 DTO、解析结果、模型载荷和联邦资源投影均 MUST NOT 复制或输出该根

#### Scenario: 原生文件附件在消息创建时取得统一引用

- **WHEN** 用户以工作区内相对路径、绝对路径或 files URI 添加同一文件附件
- **THEN** 消息创建先经同一文件 owner 取得规范 URI，返回 DTO、Job 输入、SSE、canonical 附件字段及系统生成的模型标记使用同一引用；默认显示名取文件名，不复制原生输入路径。工作区外绝对路径沿附件既有能力明确拒绝，不改投工作区内同名文件；旧 Session 附件由原附件目录 owner 按迁移来源绑定校验和读取，不重建其正文或归属
- **AND** 用户主动写在消息正文中的路径字面量原样保留；预览失败继续明确报告资源身份与错误原因，最终文件访问的系统异常保留 errno 和服务端 cause，不把物理 locator 复制进持久化诊断

#### Scenario: URI 允许悬空

- **WHEN** 一条持久化的 URI 指向的资源配置已被删除
- **THEN** 该 URI 仍是合法值，读取时返回结构化「未找到」拒绝码，而不是把 URI 判定为格式非法

#### Scenario: 界面偏好的文件引用必须持久化 URI

- **WHEN** 文件预览页签、活动文件或文件树展开态写入、读取 Gateway 用户 profile
- **THEN** 文件引用只接受中央 grammar 校验的 files URI，保留规范编码、固定目标与合法悬空引用；原生路径输入须先经文件 owner 取得 URI，再保存展示偏好。旧路径载荷明确失败且不改写原 profile，不猜目标工作区、不静默丢弃、不以空字符串代替目录根
- **AND** 该入口的请求校验和 profile 冷读失败只向普通响应报告字段与错误类别，不复制真实位置；拒绝保持明确，附带同一 request_id

#### Scenario: revision 不进入 URI

- **WHEN** 系统需要表达某资源的精确 revision 或 hash
- **THEN** 它使用与 URI 并列的独立 revision/hash 字段，绝不把 revision/hash 编码进 URI 字符串

#### Scenario: identity 不承担寻址职责

- **WHEN** 调用方持有 ResourceIdentity 但需要访问资源
- **THEN** 它仍然通过 URI（或 URI 解析链）定位资源，不得把 identity 当作可解析地址；反之 URI 也不得被当作资源身份用于去重

### Requirement: 普通文件入口必须保留原生路径输入并统一目标解析

普通文件入口 MUST 接受相对路径、绝对路径和资源 URI，保留既有文件访问能力。三种输入 MUST 经同一目标解析边界交给同一文件 owner 的操作链路，不得保留独立的旧路径执行链。资源 URI 的语法、身份、能力与授权规则继续使用本 capability 的唯一定义。

文件树 MUST 保留未注册目录的浏览和文件编辑能力，快捷目录也不得因 URI 改造而失去该能力；不得把「未注册为工作区」作为统一拒绝理由。该目录不是工作区，不得伪造 workspace_id 或自动注册。其内部引用、普通响应与快捷方式持久化所需的规范寻址由本 owner 统一补齐，再迁移消费者；不得在模板尚未接通时通过删除功能来满足真实路径不外泄检查。

- 相对路径 MUST 基于明确选定的目标工作区或目录解析；调用执行前固定目标 Gateway、工作区/目录及所属机器，不得借用 source、relay 或进程当前目录。缺少解析基准时 MUST 明确失败。
- 绝对路径 MUST 由目标机器按原生文件系统语义解释，包括 Windows 盘符、分隔符及目标平台支持的其它原生绝对路径形式。工作区内路径可归一为对应资源 URI；工作区外路径 MUST 按既有能力和授权访问指定对象，或明确拒绝，MUST NOT 去掉根/盘符后拼入工作区、改投本地同名文件，或以 URI 化为由要求先注册工作区才能使用既有外部文件能力。
- 原生路径是具名的请求输入例外，只用于固定目标后的解析与执行；必要的中继只向该目标转送输入，不在中继端进行文件系统解析或保存为资源引用。内部持久引用、普通响应、解析结果及跨 Gateway 资源投影继续遵循 URI 合同，不得因接受路径输入而返回或持久化真实路径。没有现成 URI 模板的外部目标沿既有 owner/API 能力处理，不得自行扩展 URI kind 或伪造工作区归属。
- 用户原生绝对路径与 DeepAgents 等 backend 的内部虚拟路径 MUST 有明确不同的入口或类型；内部虚拟根的转换不得应用于用户路径。全量迁移直接/间接调用方及测试后 MUST 删除歧义回退，不保留按同一个前导斜杠猜测两种语义的分支。
- 终端与 shell MUST 保留目标操作系统的原生路径语义，不自动把路径或命令改为 URI。Gateway 原生目录选择与工作区注册仍是独立管理入口；它们的输入例外 MUST NOT 被解释为禁止普通文件入口接受绝对路径。

#### Scenario: 三种输入定位同一文件

- **WHEN** 对明确目标工作区内的同一文件，分别以相对路径、目标机器绝对路径和对应资源 URI 执行读取或授权写入
- **THEN** 三者到达同一 owner、访问同一实际文件并遵守相同能力/冲突规则；结果使用同一规范资源引用，写入不产生其它目标的副本

#### Scenario: 工作区外绝对路径不能重定向

- **WHEN** 工作区为 `/work/project`，输入 `/etc/hosts`，且工作区内另有 `etc/hosts` 作为对照文件
- **THEN** 读取或写入仅按目标端能力和授权访问真正的 `/etc/hosts`，或明确拒绝且零写入；MUST NOT 读取或修改 `/work/project/etc/hosts`，也不得伪报成功

#### Scenario: 原生目录名包含协议样式字符

- **WHEN** POSIX 文件目录含冒号，用户以 `./build://debug/probe.txt`、对应原生绝对路径或文件 owner 返回的资源 URI 读取或写入
- **THEN** 三者访问同一实际文件；不能仅因原生路径中含 `://` 就将其交给 URI grammar。以协议形式开头的 URI 输入仍由唯一 grammar 校验，未知协议和非法资源 URI 明确拒绝，不回退为文件路径

#### Scenario: 相对路径不依赖中继当前目录

- **WHEN** 相对路径的目标目录已明确，而 source 或 relay 进程的当前目录不同或发生变化
- **THEN** 读取与写入仍命中选定目标目录下的同一文件，缺少基准时明确失败，不猜测当前工作区

#### Scenario: 未注册目录仍可浏览和编辑

- **WHEN** 用户在文件树选择目标机器上尚未注册为工作区的目录，浏览其中的文件并执行已有授权范围内的编辑，随后保存或重开快捷目录
- **THEN** 目录浏览、文件内容和编辑结果均来自该实际目录，快捷目录仍可定位同一对象；不要求或暗中创建工作区注册，不改投当前工作区，普通输出与快捷方式持久引用遵守统一 URI 合同

#### Scenario: 远端路径由目标平台解释

- **WHEN** 经 Gateway 向远端目标提交原生绝对路径，包括 Windows 目标上的盘符路径，而 source/relay 机器存在同名文件或采用不同操作系统
- **THEN** 路径只在目标机器按其平台语义解析与执行；目标文件的内容/变更可核实，source/relay 文件保持不变，输出与持久引用不携带真实路径

#### Scenario: backend 虚拟路径不冒充用户绝对路径

- **WHEN** backend 使用内部虚拟路径 `/src/main.mjs`，而用户也可输入原生绝对路径
- **THEN** 虚拟路径仅由明确的 backend 转换入口解释，用户输入保持目标机器绝对路径语义；不得共用去除前导斜杠的回退来猜测归属

### Requirement: 原生目录根绑定必须由目标 Gateway 唯一持有

未注册目录使用 `boxteam://{gateway_authority?}/gateway/{gateway_id}/resources/files/{directory_id}`；`gateway_id` 必须来自目标 Gateway 的真实身份，`directory_id` 是该 Gateway 创建的稳定不透明 UUIDv7 目录绑定 ID，不得由绝对路径、盘符、host、hash 或工作区身份编码而来。其后文件 child 段沿用中央 grammar 的 UTF-8 一次解码规则；不新增 scope、kind 或另一套 parser。工作区内目标优先使用既有 `workspace/.../files/root` URI。

目标 Gateway 控制面 MUST 唯一持久化 `(gateway_id, directory_id, native_root)`，以保留用户选定目录及快捷方式跨重启重开能力。此项是具名的控制面根绑定例外，不是普通业务资源路径许可；Workspace、会话快捷方式、文件 operation、目录 revision、分页 cursor、SSE、模型与联邦 projection 只保存或传输规范 URI。目录绑定不得加入工作区注册表、创建 `.boxteam/`、伪造 workspace_id、启动新工作区或自动注册。Gateway 不枚举文件、不编辑正文、不另建文件执行 owner。

目录选择/路径归一入口 MUST 先固定目标 Gateway 与已有执行路由，在目标端按原生语义解释用户相对/绝对路径；相对路径以明确的工作区或目录 URI 为基准。该入口只返回规范 URI、显示名与能力，不返回 native_root。创建和使用绑定沿用当前文件入口的身份认证与目标端文件权限，不从目录选择推导工作区管理授权。原生管理入口的工作区注册合同保持独立。

实际浏览、编辑和已有集合操作 MUST 继续由目标机器上的同一 `WorkspaceService` / `WorkspaceFileOperations` 执行。执行路由所选工作区仅承载当前操作所属服务，不是该目录的资源归属；不能因执行路由或激活工作区切换重写 URI。Gateway 仅在已认证的同机控制面调用链中将根临时交给该 owner；客户端提供的内部绑定载体必须被移除，不能覆盖控制面事实。根不得出现在浏览器响应、模型、联邦消息或 source/relay 持久化中。远端由其原生 Gateway 解析目录绑定；source/relay 不解释目标路径、不用自己的根代执行。

后台 operation 和 watch 恢复 MUST 从持久 URI 向同一原生 Gateway 取得当前绑定再执行，不依赖请求内临时缓存跨重启存活，也不把 root 写回业务库。绑定缺失、Gateway 身份不符、路由不在同机或能力不足时沿既有寻址拒绝域明确失败；不得扫描目录、猜测根、回退当前工作区或盲重放已发生的写入。URI 仍允许悬空。

#### Scenario: 未注册目录的浏览编辑与快捷方式重开

- **WHEN** 用户选择工作区外的真实目录，浏览并编辑文件，保存快捷方式后重启 Gateway/Backend 再打开该快捷方式
- **THEN** 同一目录 URI 仍访问原目录及编辑后的内容；工作区注册表和目录内 `.boxteam/` 均无新增；真实根只存在原生 Gateway 的目录绑定，快捷方式、文件操作记录、响应、cursor 和事件不含该根

#### Scenario: 跨 Gateway 目录只在原生目标执行

- **WHEN** source/relay 与目标机器存在同名不同内容文件，用户按目标原生路径选择目录并以其 URI 浏览或编辑
- **THEN** 绑定与文件操作均在目标机器完成，source/relay 文件与绑定存储不变；回包只含 URI 与内容，不能携带目标根或把目录注册成工作区

#### Scenario: 后台恢复不依赖上一请求的真实路径

- **WHEN** 文件 operation 已受理后 Backend 重启，或 watch/快捷方式在新进程恢复，而请求内根缓存已消失
- **THEN** owner 按原 URI 向同一原生 Gateway 取绑定并继续原操作的恢复合同；绑定缺失明确失败，原 operation 身份、冲突前置和效果知识边界保持不变，不复制真实路径到业务存储

### Requirement: Gateway 原始诊断日志管理入口保留原文

用户主动打开、受现有管理权限保护的 Gateway 原始服务日志与异常堆栈 MAY 保留真实文件路径，MUST 保留原始排错信息。经联邦诊断入口传递的同类原始日志适用本例外，MUST 沿用既有管理授权与目标路由边界，不降低权限要求。

本例外仅覆盖该管理入口的原始日志/堆栈及其传输，不授权把其中路径作为资源引用、普通业务错误字段、解析结果或其它持久业务记录使用。普通业务响应与资源投影继续遵守三层职责和位置判据，MUST NOT 将任意诊断摘要或整个 diagnostics 对象视为例外。

#### Scenario: 本地与联邦管理诊断保留原始堆栈

- **WHEN** 用户通过既有管理权限主动打开本地 Gateway 原始日志，或通过联邦诊断打开目标 Gateway 同类日志
- **THEN** 日志与堆栈可以包含原始真实路径，保留原文及目标来源；联邦入口继续遵守同一管理授权边界，不借此开放普通资源读取或绕过授权

#### Scenario: 原始日志例外不扩大普通错误响应

- **WHEN** 同一原生错误需要形成普通业务响应、资源引用或持久业务状态
- **THEN** 普通链路仍按原 owner 合同报告资源身份、失败阶段与原因，不把原始日志路径复制到这些记录；完整异常仍可供具名管理日志入口排错

#### Scenario: 调试方案 fork 的文件发布失败

- **WHEN** 调试方案 fork 在 staging、发布、落盘验证或重放检查中遇到原生文件错误，或发现目标布局冲突
- **THEN** 原 owner 保留异常类别、失败阶段与必要的 errno/winerror；普通响应和创建记录不包含真实路径，完整原异常及位置仍进入诊断。原 canonical 历史、来源方案和 hash 不改写，未发布目标按原 journal 清理；中断恢复仍沿原冻结 proof 与唯一发布链完成，不借此新增 writer、URI kind 或兼容路径

#### Scenario: fork 冻结清单扫描失败

- **WHEN** fork 捕获或恢复核对冻结文件清单时，清单根缺失/不是普通目录、存在符号链接或非普通文件，或目录扫描、正文读取、元数据访问失败
- **THEN** 同一清单 owner 明确拒绝，扫描失败不得被忽略或解释为空目录/残缺清单；原生错误保留异常类别、errno/winerror及cause，普通错误不包含真实路径，原位置留管理诊断。失败不改写来源正文或发布残缺目标；合法空目录、既有排除项、有序相对引用及hash规则保持，恢复仍只核对原冻结文件

#### Scenario: fork captured marker 的发布读取或清理失败

- **WHEN** captured marker owner 的位置检查、发布遍历、文件或目录 fsync、marker 写入/rename、marker/manifest/control 读取、附件检查或清理遇到原生 I/O 失败，或发布遍历发现符号链接目录
- **THEN** 原 owner 明确失败，原生错误保留类别、errno/winerror 与直接 cause，普通错误及创建记录只含逻辑 artifact 和阶段，真实位置与完整原错留诊断。扫描错误不得被忽略，失败不改写来源 canonical 字节或发布残缺目标；原 marker 编码/hash、journal 与唯一恢复链保持。没有完整 marker 的 preparing capture 与已冻结 journal 缺 marker 仍按原合同区分；完整 marker 已发布但 journal 尚未 CAS 时，恢复只验证原冻结 proof，不重新捕获来源，清理失败不声称已释放

#### Scenario: fork typed detail 复制失败仍保留原冻结合同

- **WHEN** fork 按冻结 SQLite 的声明复制 typed detail 时，发现非法目录/文件布局，或元数据、创建、复制及正文读取遇到原生错误
- **THEN** 同一复制 owner 明确失败，普通错误和创建记录不包含真实路径；原生错误保留类别、errno/winerror 与直接 cause，布局拒绝保留原因，真实位置留诊断。失败不改写来源 canonical 或 detail 正文，不发布残缺目标；重试和冷读保持原 detail 身份、正文及 request_hash。缺失正文仍交原资源 owner 判断是否必需，原必需正文的损坏/缺失拒绝不因本次错误出口调整而放宽，声明顺序、来源归属、hash 与唯一恢复链保持

#### Scenario: 隔离 SQLite worker 的启动失败、非零退出和超时

- **WHEN** fork 捕获的隔离 SQLite worker 在父进程原生调用处失败、真实子进程非零退出，或超过原30秒预算
- **THEN** 普通响应与创建记录只带逻辑 artifact、失败阶段及必要退出码/异常类别/errno，不包含解释器或文件路径；父进程原生错误保留类别与直接 cause，子进程非零退出保持原 RuntimeError 并以保存原 command/stdout/stderr/status 的 CalledProcessError 为 cause，完整 stderr 留诊断；超时保持 TimeoutExpired、原期限与原异常 cause，worker 已终止并等待后再清理私有目录。失败不改写来源 canonical 或发布残缺目标；成功仍按原固定读事务/backup和有序文件长度/hash manifest，持 rollout 锁的源页/WAL/SHM不变，session-control 在线读取只允许 SQLite 原有 SHM 读标记变化，不改变持久页/WAL；重试及冷读保持历史检查点、owner、正文和 request_hash

#### Scenario: SQLite 私有目录创建或清理失败

- **WHEN** 隔离 SQLite snapshot 的私有目录创建或清理遇到原生错误，包括worker非零退出后又遇到清理失败
- **THEN** 原owner保留原生异常类别、errno/winerror与直接cause，普通响应/创建记录只含逻辑artifact和创建/清理阶段，真实位置与完整原错留诊断。创建失败不启动worker，清理失败不能返回成功manifest或声称已释放；原finally仍调用同一TemporaryDirectory的cleanup。组合故障中worker原退出码、stdout/stderr/command与RuntimeError/CalledProcessError在清理失败的原始cause/context及诊断中保留，不得被清理错误覆盖而丢失。失败不改写来源正文或发布残缺目标，故障解除后原产品入口可重试并冷读同一历史身份、检查点、assembly与hash

#### Scenario: rollout 来源捕获失败后清理再次失败

- **WHEN** rollout 来源捕获已失败，原owner删除同一captured_root又遇到原生文件错误
- **THEN** 原清理调用明确失败，不使用ignore_errors将未删除目录当作已释放；异常类别、errno/winerror与直接cause保留，此前捕获失败保留在原生context和诊断中，普通响应/创建记录仅含逻辑artifact与cleanup阶段，不含真实位置。清理成功时仍抛原捕获错误，不改变正常复制、锁释放、manifest/hash、journal或来源字节；原创建owner继续协调中止，故障解除后的新请求可重试并按历史身份冷读检查点、assembly及request_hash，不发布半成品或增加清理链

#### Scenario: rollout 来源捕获的直接原生 I/O 失败

- **WHEN** fork 来源布局检查、私有目录准备与权限限制、既有锁复制、rollout 正文复制、SQLite 副本检查、JSONL 冻结、control 读取或 manifest 发布遇到原生错误
- **THEN** 同一捕获 owner 明确失败，普通错误与创建记录只包含逻辑 artifact、原失败阶段及必要异常类别/errno，不含真实位置；原生错误保留类别与直接 cause，ACL 原语已有的 RuntimeError 及原启动/超时原因链保持，真实位置与完整原错留诊断。失败不改写来源正文或发布残缺目标，故障解除后原产品入口可重试并冷读原检查点、metadata、pending writes、历史 assembly 及 request_hash；原 reader/worker、typed detail、锁、复制顺序、manifest/hash 与 journal 保持

#### Scenario: 来源捕获目标目录已属于既有请求

- **WHEN** 当前请求创建 captured_root 时目录已经存在
- **THEN** 同一原生创建操作以 FileExistsError 明确失败并保留直接 cause，普通错误不含真实位置；当前请求不删除或改写该既有目录及其正文，不把未取得的目录所有权当成可清理对象

#### Scenario: fork 冻结产物导出遇到扫描或读取失败

- **WHEN** 来源导出冻结 artifact 清单时根或嵌套目录扫描、文件元数据访问失败，发现符号链接目录，或原分块读取失败
- **THEN** 原导出 owner 明确失败，扫描错误不得被忽略、视为空目录或用于成功返回残缺清单，符号链接目录不得静默略过。原生错误保留类别、errno/winerror与直接cause，普通错误及创建/capture记录只含逻辑artifact及export_manifest/export_chunk阶段，不含真实位置；真实位置与完整原错留诊断。失败不改变来源canonical字节、不读取链接目标或发布半成品，原两端journal完成中止及释放；解除故障后原产品入口可重试并冷读同一检查点、metadata、pending writes、历史assembly和request_hash。原分页顺序、分块边界、offset/hash、来源/目标身份、接收并发与唯一清理链保持

#### Scenario: fork 冻结产物目标接收发生原生 I/O 失败

- **WHEN** 目标接收冻结文件时位置检查、目录准备、文件打开、写入、同步、关闭或文件/整份artifact目录发布失败
- **THEN** 同一接收owner保留原生类别、errno/winerror及直接cause，普通错误和创建/capture记录只含逻辑artifact与receive阶段，不含真实位置；真实位置及完整原错留诊断。写入后关闭再次失败保留两份异常及原cause/context，不丢失首错；原stream仍由同一finally关闭，原import owner回收全部接收任务后清理。两端原journal中止、不发布半成品、不改写来源canonical；故障解除后原产品入口重试保持历史检查点、metadata、pending writes、assembly/owner与request_hash，原接收顺序、块校验及有界并发保持

#### Scenario: fork 接收等待中的来源传输失败保留原归属

- **WHEN** 接收者等待来源transport时连接失败或被取消，且本机流关闭正常
- **THEN** 原传输异常对象或取消原样传播，不被本机文件错误上下文重新包装，也不误标为receive阶段；本机stream仍按原生命周期关闭，失败和清理继续沿原import/capture owner协调

#### Scenario: fork 目标暂存与准备同步失败

- **WHEN** 冻结 rollout 在目标暂存创建、原字节复制、失败清理或准备后文件/目录同步时遇到原生错误，扫描失败，或发现缺失根、符号链接和非普通文件
- **THEN** 原目标安装 owner 明确失败；扫描错误不得被忽略或当作完整同步。普通错误和创建记录只含逻辑 artifact=fork_target 与 staging/staging_copy/staging_cleanup/preparation_sync 阶段，布局/hash拒绝不输出内部 locator；原生类别、errno/winerror、cause 及清理双错的原因链保留，真实位置只留诊断。创建碰撞不删除既有暂存目录；失败不发布半成品或改变来源字节，原创建 owner 完成中止，解除故障后原产品入口新请求可重试并冷读同一 checkpoint、metadata、pending writes、assembly/owner 与 request_hash。复制块大小、hash、SQL 与 bottom-up 同步/发布顺序保持，不增加安装或恢复链

#### Scenario: fork 目标发布、失败回滚或恢复遇到原生错误

- **WHEN** 原目标安装在 target/rollback/stage rename、目录同步、重入恢复检查/还原/清理或最终 rollback 删除时失败，包括安装失败后回滚再次失败
- **THEN** 同一目标 owner 明确失败；普通错误与创建记录只含逻辑 artifact=fork_target 和 publication/publication_recovery/publication_finalize 阶段，不包含真实位置或内部 locator。原生类别、errno/winerror、直接 cause 与双错的原 context 保留，完整位置和原错留诊断；原排他锁、目录发布/同步/还原顺序、journal 和来源字节保持。原创建 owner 继续中止未发布目标；解除故障后沿原恢复链或产品新请求可冷读同一历史 checkpoint、metadata、pending writes、assembly/owner 和 request_hash，不新建安装或清理实现

### Requirement: Browser 本地文件页面 URL 保留原生语义

系统 MUST 保留 Browser 原生 `file:` 导航能力。用户明确打开的本地文件页面 URL MAY 原样保留在 Browser 地址栏、导航请求、该页面相关错误、页面快照及恢复检查点中。该 URL MUST 始终由目标机器按原生文件系统语义解释，包括 Windows 盘符与路径分隔符；中继、source 机器和当前激活工作区 MUST NOT 改写其目标或在自身文件系统解释该 URL。

本例外仅覆盖上述页面 URL 字段，不授权保存或输出 checkpoint 文件位置、下载/截图产物位置、其它 Browser 内部存储 locator、普通业务响应或资源引用。`file:` 页面 URL 不成为第二套资源 URI，不改变资源 identity、授权、普通文件三种输入和同一文件 owner/操作链路，也不新增 URI kind 或解析器。

#### Scenario: 本地页面导航与恢复保持目标端原 URL

- **WHEN** 用户在明确的目标 Browser 打开本地文件页面，随后读取地址栏/页面快照、查看导航错误或保存并恢复检查点
- **THEN** 这些入口可以保留原 `file:` 页面 URL，原目标机器按本机文件系统语义执行或明确报告原目标失败，不改投中继或当前工作区同名文件

#### Scenario: 页面 URL 例外不豁免 Browser 产物位置

- **WHEN** Browser 保存页面 URL 的同时写入恢复检查点、下载或截图产物
- **THEN** 页面 URL 按本例外保留，但 checkpoint 文件本身的位置和下载/截图文件位置仍按唯一位置判据由 owner 身份推导或既有引用访问，不借页面 URL 例外落盘或输出真实路径

### Requirement: 「位置」必须有可机械判定的统一判据

资源引用中的「位置 / location」判定 MUST 使用如下**可机械判定**的统一判据，MUST NOT 各 change 自行解释：一个字段承载的是「位置」，当且仅当它（或由它经确定性推导得到的值）可直接用于 `open`/`stat`/`join` 等文件系统或 sqlite 访问，且指向本体位于持久化记录 owner 之外的资源或产物。据此：

- **控制面根绑定例外**：工作区物理根按 requirement「工作区物理根必须绑定到唯一原生 Gateway」持久化；用户显式选定的未注册目录根按本 capability「原生目录根绑定必须由目标 Gateway 唯一持有」持久化。两类根均只由目标原生 Gateway 持有，均不属于业务资源记录，不改变下述位置判据，也不授权其它 real path 落盘。两个具名日志/Browser 页面 URL requirement 的例外按其精确边界优先适用，不改变未命中字段的位置判定。
- **可判定为「位置」⇒ MUST NOT 落盘**：任何「位置」MUST 由 `资源身份 / ResourceIdentity` 加 `资源 URI`（必要时加并列 revision 字段）表达；当且仅当该位置可由 owner 身份（资源 id 与工作区身份）确定性推导时，MUST NOT 落盘，改为在该次 fs/进程调用栈内由 owner 身份重推导。
- **不可由 owner 身份推导、但必须对外可寻址 ⇒ MUST 用既有 API 端点引用**，MUST NOT 落盘 real path、MUST NOT 为此自造 URI kind、MUST NOT 裸拼接 URI。
- **一般 owner 运行态字段**：除上述 Browser 页面 URL 的具名例外外，owner **自身**的运行态字段（典型为终端 shell 的当前工作目录）表达的是 owner 的运行状态而非「资源所在位置」，MUST NOT 被当作位置引用或位置表达；此类字段 MUST 以**相对于工作区根的原生相对路径**持久化，允许以 `..` 表达工作区外 cwd 并保留原目标，MUST NOT 落盘绝对路径或工作区根路径。
- **real path 出现在一般持久化记录、API 响应体或系统生成的模型资源引用中 ⇒ 缺陷**：具名例外除上述两类原生 Gateway 控制面根绑定外，仅包括 requirement「Gateway 原始诊断日志管理入口保留原文」与「Browser 本地文件页面 URL 保留原生语义」明确列出的入口和字段；未命中者仍受本条约束。原生目录选择/注册与普通文件路径请求的具名输入边界引用前述 requirements，不授权将真实路径复制到业务记录、普通响应 DTO、解析结果或联邦资源投影。违反时 MUST 显式失败，MUST NOT 以脱敏、截断或 `display_uri` 静默掩盖。

以上判据 MUST 是「位置 vs 非位置」的唯一判定来源；`add-workspace-persistent-resource-management` 的 D-A2/D-A3 与 owner 运行态字段例外 MUST 引用本 requirement，MUST NOT 另立一套判据。

#### Scenario: 可由 owner 身份推导的位置不落盘

- **WHEN** 一条持久化记录原本需要表达一个可由 owner 身份（资源 id 与工作区身份）确定性推导的资源或产物位置
- **THEN** 该位置 MUST NOT 进入持久化记录，改为在该次调用栈内由 owner 身份重推导；MUST NOT 写入绝对路径或等价的文件系统路径

#### Scenario: 会话文件变更的快照位置由 edit 身份推导

- **WHEN** 会话变更 owner 保存、聚合或输出文件修改及其 before/after/diff 快照
- **THEN** 修改对象只引用 files URI；快照由 session/edit 身份和阶段在 owner 的访问调用栈内定位，索引、metadata、changeset 摘要和工具事件不再保存或输出 before_file、after_file、diff_file。公开响应仍提供 diff_text 与 before/after preview；已发布字段编号与名称 reserved，不保留旧读取或兼容别名

#### Scenario: 变更记录和审查状态冷读不得恢复旧路径

- **WHEN** owner 冷读文件修改索引或文件审查状态，或写入新的记录
- **THEN** 同一引用校验保留合法悬空的 Workspace/Gateway 文件 URI、规范编码与固定目标，不在 source 查找同名文件。原生路径、相对路径、非文件 URI、目录根、旧快照位置字段、非法审查状态与冲突的规范 URI 必须明确失败；原记录保持不变，不猜目标、不静默合并或改写

#### Scenario: 不可推导但需寻址时用既有 API 端点

- **WHEN** 一条持久化记录或模型可见载荷需要对外寻址一个无法由 owner 身份推导的产物
- **THEN** 可寻址性 MUST 由承载该能力的既有后端 API 端点引用提供；MUST NOT 自造 URI kind、MUST NOT 裸拼接 URI、MUST NOT 暴露文件系统路径

#### Scenario: owner 自身运行态字段以相对于工作区根的原生相对路径持久化

- **WHEN** 持久化记录需要保存 owner 自身的运行态字段（典型为终端 shell 的当前工作目录）
- **THEN** 该字段 MUST 以相对于工作区根的原生相对路径持久化，工作区外 cwd MUST 保留 `..` 所表达的原生目标，MUST NOT 改投工作区内目录或落盘绝对路径、工作区根路径；该字段 MUST NOT 被当作「资源所在位置」的位置引用，也 MUST NOT 被扩张解释为「任何位置都可改用文件系统路径代替 URI」

#### Scenario: 物理工作区根只作为原生 Gateway 控制面绑定持久化

- **WHEN** 一个工作区根不能由 `workspace_id` 推导，且必须供原生 Gateway 恢复 mount
- **THEN** 它 MUST 只按 requirement「工作区物理根必须绑定到唯一原生 Gateway」的合同持久化于唯一 Gateway binding；该基础设施绑定不是业务资源位置字段，MUST NOT 扩展为其它 real path 的持久化许可

#### Scenario: 判据可机械判定

- **WHEN** 审查一个字段是否属「位置」
- **THEN** 判定只依赖「能否经确定性推导直接用于文件系统 / sqlite 访问」与「是否属于 owner 自身运行态字段」两项可机械检查的事实，MUST NOT 依赖调用方解释或领域术语

#### Scenario: 配置应用事件的路径字段不是「位置」

- **WHEN** 配置应用事件与配置来源响应报告 `changed_paths` / `applied_paths` / `deferred_paths`
- **THEN** 这些值的口径 MUST 是配置 JSON 的 **JSON Pointer**（以 `/` 起的分段键路径，由 `app/services/infrastructure/config/state.py` 的 `changed_json_paths` 单点产出），表达「哪一项配置键变了」，MUST NOT 是文件系统路径或工作区 real path；故它们按本 requirement 判据**不属「位置」**，不适用 URI 表达与「不得落盘」义务，也 MUST NOT 被新增用途扩大解释为可承载 real path

### Requirement: scope 必须取自定稿闭集且 scope_id 对所有 scope 必填

URI 的 scope MUST 取自**定稿闭集** `workspace` | `user` | `gateway` | `inline`（依据权威表：`builtin` 正名为 `inline`；`user` 为本次新增；`memory` 已移出）。该闭集与每个 scope 的 scope_id 取值来源 MUST 由本 capability 的**唯一一张表**规定，其它模块与 change MUST NOT 自行发明 scope 名或 scope_id 语义。该表 MUST 与 `add-multi-workspace-backend-mounting` 的挂载模型保持一致（其 `workspace` scope 的 scope_id 与显式 HTTP 寻址的 workspace_id 同源）；本 capability 与该 change MUST NOT 各自定义 scope_id 取值语义，取值规则一律以本表为唯一出处。

**`memory` 已确证不是 URI scope，MUST NOT 出现在闭集内**：它零生产构造方、resolver 连 scope_id 都不比对、`kind="memory"` 全仓零构造、container 未装配（原 `configs/workspace_inline.jsonc:427-433` 的 `agent.memory` 6 键配置块与 schema `$defs.agentMemory` 已随 `remove-agent-memory` 物理删除，现配置已无该块）；**`memory` 的 domain owner 与状态本体从未接入，故无 URI 替代 owner 的需求**。解析器侧 MUST 物理移除既有两点式 `boxteam://memory/{scope}/{name}`（无 `resources` 固定段、无 kind、恰好两段）的特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**：该两点式特例分支与 `memory_display_uri` 构造函数均已物理删除）；MUST NOT 把该形态当作合法 URI 接受或产出。

`scope_id` 段 **MUST 对所有 scope 都出现且必填**，MUST NOT 只对某个 scope 必填。`scope_id` MUST 由**真实身份推导**，MUST NOT 硬编码字面量，MUST NOT 依赖隐含上下文（依据权威表）：

- `workspace` → 真实 workspace_id（现状即为真实 id）；
- `gateway` → **真实 gateway_id**（取值来源与注入 owner MUST 按本 capability 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」定稿落地）；
- `inline` → **真实 distribution_id**（修订前现状为与 `gateway` 逐字共用字面量 `local`、`distribution_id` 全仓零生产赋值，属既有不一致；**已由 `298ef599`+`f3bd8213` 落地改为按 manifest 推导，2026-10-01 第八轮复核更正**）；来源与编码 MUST 按本 capability 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」定稿落地； **（修订注，`298ef599`+`f3bd8213` 落地）**：现状已改为按 manifest 推导，不再与 `gateway` 共用字面量 `local`，`distribution_id` 不再是全仓零生产赋值。
- `user` → `local`，并 MUST 显式声明为**单用户本地程序的约定**（AGENTS.md 明确无云服务、无多租户），MUST NOT 虚构用户名； **（修订注，`f6fc990f` 落地）**：现状已在 `grammar.py` 的 `_SCOPE_KEYWORDS` 落地 `user` scope，`user` 的 `scope_id` 由 `app/core/user_identity.py::user_scope_id` 单点返回 `local`（与 `inline` 的 `distribution_identity`、`gateway` 的真实 gateway_id 推导同族），resolve 时经 `ResolutionContext.user_scope_id` 显式携带并由 `resolver.require_scope_binding` 校验，故 `user` 不再是「parse 成功而 resolve 无绑定」的悬空 scope。

「当前工作区」不是寻址概念，MUST NOT 作为持久化数据的隐含前提。其它工作区 MUST 复用 `workspace` scope 加另一个 `workspace_id` 表达，MUST NOT 引入新 scope。

#### Scenario: scope_id 对所有 scope 必填

- **WHEN** 系统构造或解析任意 scope 的 URI
- **THEN** 路径中 MUST 出现显式 `scope_id` 段；任何省略 `scope_id`、或依赖「当前激活工作区 / 当前 gateway / 当前发行版」补齐缺省 scope_id 的解析一律被拒绝

#### Scenario: workspace scope 的 scope_id 是真实 workspace_id

- **WHEN** 解析一个 `workspace` scope 的 URI
- **THEN** `scope_id` 段被解释为真实 workspace_id；同 gateway 下的其它工作区就是另一个 `workspace_id`，不新增 scope

#### Scenario: gateway 与 inline 的 scope_id 由真实身份推导而非硬编码

- **WHEN** 系统为一个 `gateway` 或 `inline` scope 构造 URI
- **THEN** `gateway` 的 `scope_id` 取真实 gateway_id（而非字面量 `local`）、`inline` 的 `scope_id` 取真实 distribution_id（而非与 gateway 共用的 `local`）；后者按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」的编码规则从 manifest 推导

#### Scenario: user scope 的 scope_id 是单用户约定值 local

- **WHEN** 系统为一个 `user` scope 构造 URI
- **THEN** `scope_id` 为 `local`，并显式以「单用户本地程序」解释该值，不虚构用户名或细分身份

#### Scenario: inline 取代 builtin

- **WHEN** 系统需要表达发行包内置层的资源
- **THEN** 它使用 `inline` scope，并 MUST NOT 继续产出或接受 `builtin` 作为 scope 名，也不保留 `builtin`→`inline` 的运行时别名

#### Scenario: 未知 scope 被拒绝

- **WHEN** 解析遇到闭集以外的 scope 段（含 `memory`、`session`、`sqlite`）
- **THEN** 系统返回 scope 未登记的结构化拒绝码，且不尝试任何猜测映射

#### Scenario: memory 特例分支必须被物理移除并以 unknown_scope 拒绝

- **WHEN** 调用方提交 `boxteam://memory/{scope}/{name}` 一类两点式字符串
- **THEN** 解析器侧 MUST 已物理移除该两点式特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝；MUST NOT 按 URI 解释该字符串，MUST NOT 为其定义 scope_id

### Requirement: inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导

`inline` scope 的 `scope_id` MUST 由该发行包的 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）中的 `distribution` 与 `version` 两个字段**确定性推导**，MUST NOT 取自目录名、安装路径或任何随环境变化的量（否则跨 gateway 与跨机器寻址从根上不成立）。

**为什么需要编码**：URI 动态段的闭合 charset 为 `[A-Za-z0-9_-]`（`grammar.py:23` 的 `_NAME_CHARSET`），MUST NOT 放宽。而 `version` 的实测形态是语义化版本（发行包 `version` 取自根 `package.json` 的 `version`，实测为 `0.0.2`），**含点号 `.`，不在 charset 内** —— 直接拼接（如 `source-development-0.0.2`）会被 grammar 以「含未登记字符」结构化拒绝。因此 MUST 在 charset 内选择编码，MUST NOT 放宽 charset 或新增转义后门。

**编码规则（定稿，唯一方案）**：令 `distribution`、`version` 取 manifest 原值；`version` 的合法字符集为 `[A-Za-z0-9.-]`（即 semver 标识字符集 `[0-9A-Za-z-]` 加 `.`，**不含 `_`**——semver 的 pre-release/build 标识本就只允许 `[0-9A-Za-z-]`，`_` 从不是合法版本字符）。编码为 `.`→`_` 的**单射**映射：`scope_id = distribution + "-" + version.replace(".", "_")`。唯一实现是 `app/core/distribution_identity.py` 的 `_VERSION_PATTERN`、`encode_version` 与 `load_distribution_id`，已由 `f3bd8213` 落地。

- 实测：`(source-development, 0.0.2)` → `source-development-0_0_2`；`(npm, 1.0.0-beta.1)` → `npm-1_0_0-beta_1`。
- 单射性：因合法 `version` 已排除 `_`，编码结果中的 `_` 只可能由原点号产生，故不同 `version` 必得不同编码（穷举断言见 `tests/unit/core/test_distribution_identity.py`）。早先尝试的「先 `_`→`__`、再 `.`→`_`」双步转义**不是单射**（`_` 本就在旧 charset `[A-Za-z0-9._-]` 内，与原点号同码），已由 `f3bd8213` 删除，MUST NOT 恢复。

**MUST 同时满足的编码性质**：

- **在 charset 内**：结果只含 `[A-Za-z0-9_-]`；`distribution` 实测为闭合枚举 `source-development` / `source-installed` / `npm` / `standalone`（均在 charset 内、无点号）；
- **单射（等价于无碰撞）**：不同 `(distribution, version)` 组合必得不同 `scope_id`；该单射性由 `.`→`_` 在排除 `_` 的合法 `version` 上成立来保证，MUST NOT 依赖任何解码/还原步骤。若要由 `scope_id` 拆分回 `(distribution, version)`，拆分点 MUST 由闭集 `distribution` 枚举做**最长前缀匹配**决定，MUST NOT 用「首个 `-`」裸切（`distribution` 自身含 `-`，如 `source-development`）；枚举内任一取值都不是「另一取值 + `-`」的前缀，故拆分唯一。
- **稳定**：同一发行包在任意机器、任意安装路径下算出逐字相同的 `scope_id`（只依赖 manifest 两字段）。

**`version` 合法形态**：MUST 只含 `[A-Za-z0-9.-]`（semver 形态，**不含 `_`**）；含 `_` 或其它字符（如 `+` 构建元数据、空格、`/`）时 MUST fail-closed 显式拒绝并报出实际值，MUST NOT 放宽 charset、MUST NOT 静默丢弃或替换、MUST NOT 回退默认值。唯一实现见 `app/core/distribution_identity.py` 的 `_VERSION_PATTERN` 与 `load_distribution_id`（`f3bd8213` 已把含 `_` 的 `version` 改为显式报错，MUST NOT 重新放行）。

**缺失时 MUST fail-closed**：`distribution` 或 `version` 缺失（含空串）时，系统 MUST fail-closed 拒绝构造 `inline` 的 `scope_id` 并显式报错，MUST NOT 回退为 `local`、「当前发行版」或任何虚假默认值（AGENTS.md「永不返回虚假的默认值」）。开发态（`source-development`）也 MUST 走同一 manifest 路径，MUST NOT 为其单开默认分支。

#### Scenario: inline scope_id 从 manifest 确定性推导

- **WHEN** 系统为 `inline` scope 构造 URI
- **THEN** `scope_id` 由 manifest 的 `distribution` 与其 `version` 按上述编码规则算出（如 `source-development` + `0.0.2` → `source-development-0_0_2`），不含目录名或安装路径

#### Scenario: 带点版本号不得直接拼接

- **WHEN** `version` 含点号（如 `0.0.2`）而被直接拼进 `scope_id`（如 `source-development-0.0.2`）
- **THEN** grammar MUST 以「含未登记字符」结构化拒绝；系统 MUST 改用 charset 内的编码规则，MUST NOT 放宽 charset、MUST NOT 新增转义后门

#### Scenario: 编码单射且不同 (distribution, version) 不相撞

- **WHEN** 给定两个不同的 `(distribution, version)`
- **THEN** 它们算出的 `scope_id` 必不相同（单射）；该性质只依赖「合法 `version` 不含 `_`」这一 charset 约束，MUST NOT 依赖任何解码/还原步骤来保证

#### Scenario: 含下划线的 version 必须 fail-closed

- **WHEN** manifest 的 `version` 含 `_`（如 `1.0.0_beta`）或其它 `[A-Za-z0-9.-]` 外字符
- **THEN** 系统 MUST 显式报错拒绝构造 `inline` 的 `scope_id` 并报出实际值；MUST NOT 放宽 charset 把 `_` 纳入、MUST NOT 静默丢弃或替换该字符、MUST NOT 回退为 `local` 或任何默认值

#### Scenario: 跨机器与安装路径稳定

- **WHEN** 同一发行包安装在不同机器或不同路径
- **THEN** 算出的 `inline` `scope_id` MUST 逐字相同

#### Scenario: manifest 缺字段时 fail-closed

- **WHEN** manifest 的 `distribution` 或 `version` 缺失或为空
- **THEN** 系统显式失败并报出缺失字段，MUST NOT 使用 `local` 或任何默认值代替

### Requirement: gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导

`gateway` scope 的 `scope_id` MUST 为**真实 gateway_id**，取值来源定稿为 `${BOXTEAM_HOME}/gateway/identity.json` 中由 `app/gateway/credentials.py:138` 的 `load_or_create_gateway_id` 生成的随机不透明 id（形如 `gateway_<32hex>`）。MUST NOT 取自 host:port、监听端口或任何瞬时通道标识（channel instance/epoch/route）。

`scope_id` MUST 由**真实身份推导**，MUST NOT 硬编码字面量（现状 `skill_runtime.py:539` 的 `else "local"` 让 `gateway` 与 `inline` 逐字共用字面量 `local`，落地时物理移除）。 **（修订注，`298ef599` 落地）**：`skill_runtime.py` 的 `else "local"` 已物理移除，`gateway` 与 `inline` 不再共用 `local`（`gateway` 已由请求级注入落地（`64ba30c8`/`53befbfc`/`9881a3b2`））。

**注入 owner MUST 为 Gateway 侧**：Gateway 代理 `/api/v1/*` 时 MUST 附加 Gateway 身份头，workspace 后端 MUST 从请求上下文读入。头名 MUST 按既有 `X-BoxTeam-*` 约定命名，定稿为 `X-BoxTeam-Gateway-Id`（既有头为 `X-BoxTeam-Federation-Token`/`X-BoxTeam-Workspace-Id`，见 `app/gateway/auxiliary_proxy.py:91-92`、`app/gateway/registry.py:1837-1838`；仓库此前无 gateway 身份头）。MUST NOT 改写或复用 `X-Request-ID` 的语义与职责（AGENTS.md：任何一层不得补造第二个请求 ID）。

**架构性约束**：同一 workspace 后端可被不同 Gateway 挂载，故 gateway 身份 MUST **按请求注入并读取**，MUST NOT 用进程级单例或「当前激活」态。

**缺失或非法时 MUST fail-closed**：请求未携带 Gateway 身份头、或头值非法时，系统 MUST fail-closed 显式拒绝，MUST NOT 回退 `local` 或任何虚假默认值（AGENTS.md「永不返回虚假的默认值」）。

**消费（只登记，不实现）**：`skill_runtime.py` 的 `else "local"`（已由 `64ba30c8` 物理移除）MUST 拆为 `gateway`→真实 gateway_id、`inline`→distribution_id；`ResolutionContext` MUST 建立第一条生产构造链路。 **（修订注（2026-10-01 第四轮复核更正）：`inline` 部分已由 `298ef599` 落地；`gateway` 请求级注入已落地——`64ba30c8`/`53befbfc`/`9881a3b2`）**

**前端口径冲突（已登记影响项）**：`src/clients/web/src/state/session/sessionCatalogOutbox.ts:32` 的 `CatalogOutboxPartition.gatewayId` 注释逐字为「稳定 Gateway 身份：本地 Gateway 用其监听端口，远程 Gateway 用其 gateway_id。」，与本 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」中「MUST NOT 取自 host:port、监听端口或任何瞬时通道标识」的要求冲突。统一口径归 Gateway 侧：网关身份由 Gateway 按请求注入、取值由本 requirement 推导；该旧口径注释 MUST 在本 change 实施期清理。

#### Scenario: gateway scope_id 按请求注入推导

- **WHEN** Gateway 代理一个 `/api/v1/*` 请求给 workspace 后端，且 workspace 后端为 `gateway` scope 构造 URI
- **THEN** Gateway MUST 附加 `X-BoxTeam-Gateway-Id`；后端 MUST 从请求上下文读入该值并作为 `scope_id`（即 `identity.json` 里的真实 gateway_id），MUST NOT 取 host:port、监听端口或字面量 `local`

#### Scenario: gateway 身份缺失或非法时 fail-closed

- **WHEN** 请求未携带 Gateway 身份头，或头值不是合法 gateway_id
- **THEN** 系统 MUST fail-closed 显式拒绝，MUST NOT 回退 `local`、MUST NOT 用进程级单例或「当前激活」态补齐

#### Scenario: gateway 身份按请求注入而非进程级单例

- **WHEN** 同一 workspace 后端进程被两个不同 Gateway 挂载并分别收到请求
- **THEN** 两次请求各自读入**自己** Gateway 注入的身份并得到各自的 `gateway_id`，MUST NOT 命中任何进程级「当前 gateway」单例

### Requirement: URI 语法必须保留固定段序并单一实现

统一 URI 语法 MUST 为：

```text
boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}
```

可选 authority 承载稳定 gateway_id；scope_id 对所有 scope 必填，`resources` 是固定保留段，kind 取自本规范闭集。系统 MUST 由唯一寻址 owner 根据 kind 的根模板区分结构/身份段和文件路径段，不得将整条 URI 套用同一个动态段字符表。结构关键字必须精确匹配大小写；身份段继续使用其闭合 ASCII profile（通用动态身份段为 `[A-Za-z0-9_-]`，业务 ID 同时遵守自身 profile），不得百分号解码、折叠大小写或用文件名规则放宽身份校验。

文件路径段 MUST 支持点号、中文、空格及字面百分号等正常文件名。构造器接收未编码文件名，规范 URI 将每段 UTF-8 字节按 RFC 3986 编码：仅 unreserved 字符直接输出，其余使用大写 `%HH`，空格为 `%20` 而非 `+`。解析 MUST 先按字面 `/` 分段，再对已确定的文件段严格解码一次、验证后按相同规则重编码；合法小写 escape 或 encoded unreserved 可归一到唯一形式。MUST NOT 二次解码、Unicode 归一化或大小写折叠文件名，避免改变目标文件。编码后的 `/` 或反斜杠、解码后的空段、`.`/`..`、NUL/控制字符、无效 UTF-8 和残缺 escape 必须在访问前拒绝。解码后字面 `%2e` 只是文件名字符，不再解释成点号。URI 本体禁止原始非 ASCII、空格、反斜杠、userinfo、query、fragment及冗余/尾随 `/`；文件名中的 `?` 和 `#` 必须编码。

文件路径 profile 只用于根模板公布的 child path；Session ID、assembly 引用、配置逻辑名和其它领域尾段不得因此变成文件路径。既有 `.../resources/skills/{name}/SKILL.md` 仍由同一规则解析；技能根及多层 child 形态见下一 requirement。authority 解析仍需任务3.4的生产实施，本修订不声称已接通。

#### Scenario: 固定段和身份规则不被文件名规则放宽

- **WHEN** URI 缺少 resources、缺少 scope_id，或身份段包含百分号编码、Unicode、非法业务 ID
- **THEN** 系统在资源访问之前按中央 grammar 拒绝，不补当前工作区，不将身份段当文件名解码

#### Scenario: 技能多层文件与普通文件名往返

- **WHEN** 构造 `references/中文 文档.v2.md`、`scripts/run.py`、`100%.txt` 等文件的 URI
- **THEN** 构造、解析和结果引用使用同一 UTF-8 分段编码；解码后逐字匹配文件名，规范 URI 可以再次访问同一目标

#### Scenario: 非法 escape 和路径穿越在访问前失败

- **WHEN** 文件段含残缺 escape、无效 UTF-8、编码分隔符，或解码后为 .、..、控制字符
- **THEN** 系统按中央 grammar 显式拒绝，未打开文件、未注册来源或产生写入

#### Scenario: 字面百分号不被第二次解码

- **WHEN** 文件名为 `%2e%2e.txt`，其规范 URI 使用 `%252e%252e.txt`
- **THEN** owner 只定位该字面文件名，不继续解码，不解释为相对段

#### Scenario: query 和 fragment 与文件名区分

- **WHEN** URI 有原始 query/fragment，或合法文件名含已编码的问号/井号
- **THEN** 前者明确拒绝，后者仅在文件段按一次解码规则访问，不产生 view/revision selector

### Requirement: kind 闭集定稿且描述符闭集独立不可混用

URI kind 闭集 MUST 为 `agent-spec` | `skills` | `config` | `session` | `files` | `attachments` | `workspace` | `folder`。`attachments` 只允许 workspace scope，目录根为 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/attachments/{attachment_id}`，其后按同一文件路径段规则访问 child；原件与预览只读，派生区复用原文件 owner 的写入能力。`files` 用于 `workspace` scope 的文件树根 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/files/root`，以及 requirement「原生目录根绑定必须由目标 Gateway 唯一持有」登记的 `gateway` scope 原生目录根；`workspace`/`folder` 沿用现行领域导航模板。config 定位配置逻辑来源，session 定位领域对象或由原 owner 公布的产物引用，二者不得被文件工具自行解释成私有目录。

语法 kind 与语义来源描述符 kind MUST 分开：描述符仍只有 `agent-spec`/`skills`，普通 files 导航不会创建来源描述符。后续扩展必须先在本 owner 更新模板和能力，不存在通用未知 kind fallback；旧 memory 两点式仍拒绝。

#### Scenario: 工作区文件树有正式根

- **WHEN** 用 files/root 定位某个已明确指定工作区的源码树
- **THEN** owner 以 URI/身份解析到目录根并返回目录 shape、规范 URI 与操作能力，可按公布的 child path 规则导航；解析结果和普通 DTO MUST NOT 返回物理根，实际文件访问只在原生 owner 本地使用 root binding，且不依据当前激活工作区替换 scope_id

#### Scenario: 未登记 kind 或 scope 组合被拒绝

- **WHEN** 使用未登记 kind 或在 user/gateway/inline scope 使用 workspace files 根
- **THEN** owner 显式拒绝，不回退为本地普通路径

#### Scenario: 描述符闭集不因目录导航扩张

- **WHEN** 普通 files 目录列举或搜索返回许多文件
- **THEN** 系统不将 files 加入语义来源描述符闭集，也不逐文件建立来源或长期身份

### Requirement: 导航形态与操作能力必须由资源 owner 公布

资源根 MUST 公布 `resource_shape=directory|file|domain_object` 和允许的 operations。仅 directory 根或其真实目录子节点、且公布 child path 规则时可追加相对文件段；file 节点不可追加子路径，domain_object 按 owner 给出的引用、结构化 view 和领域操作访问。导航能力不等于写权限；固定修订文件和受 owner 管理的产物不可由通用写工具覆盖。根定位和读取必须校验实际 containment，越界 symlink 不得成为访问另一根的后门。 工作区 files 根也不得绕过业务 owner 导航或修改其私有存储（例如 .boxteam 下的会话、catalog、checkpoint 和运行状态）；这类路径按 owner 能力拒绝，不以禁止所有点号目录代替该边界。

技能包根为 `.../resources/skills/{name}`，name 仍是 catalog 的身份段；其后是文件路径段，可定位 SKILL.md、references、scripts 和其它真实多层文件。工作区文件树使用本 owner 登记的 files/root 根。附件目录使用本 owner 登记的 workspace attachments 根；原件为其 `original/content` child，预览和处理结果仍在同一根内。agent-spec 和 config 的现有逻辑地址是单文件引用；session/assembly 是领域对象，工具结果由原 owner 在 `session/{session_id}/tool-results/{result_id}` 模板下公布单文件引用。旧 `session/{session_id}/attachments/{attachment_id}` 模板仅保留封存历史解析，各身份段不是任意文件路径，不用于新上传；全部模板遵循统一 scope/authority 语法，不公开底层私有存储树。根名与 URI 模板由本 owner 固定登记，不运行动态插件注册。

#### Scenario: 目录结果可继续访问

- **WHEN** 列举技能包根或工作区源码树的多层目录
- **THEN** 每个返回节点带实际 shape 和可继续使用的规范 URI；无需先为该文件登记独立长期身份

#### Scenario: 领域对象不是可遍历目录

- **WHEN** 对 Session、assembly 或单文件节点追加猜测路径并尝试列目录/写入
- **THEN** owner 按导航/操作能力明确拒绝，不推导 SQLite、checkpoint 或私有目录位置

#### Scenario: 私有存储不能借原生根或别名绕过 owner

- **WHEN** 普通 files 入口通过相对路径、原生绝对路径、工作区 URI、Gateway 原生目录根或符号链接访问保留的 `.boxteam` 存储树
- **THEN** 共用文件 owner 在内容读取或修改前明确拒绝，目录结果不公布私有树及其别名；其它点号目录不因名称以点号开头而被禁止
- **AND** 递归复制、上传和目录下载同样不得携带或覆盖私有子树，目录下载在进入私有子树前拒绝；已受理操作的来源变成私有别名时，按原操作记录拒绝发布，不继续读取或复制该来源
- **AND** skills、配置和 Session 产物等仍可通过各自 owner 公布的精确资源能力访问，files 边界不取代领域 owner 的访问合同

#### Scenario: 读取技能入口不激活

- **WHEN** 通用 read 通过技能包文件 URI 读取 SKILL.md
- **THEN** 返回有界文件内容，不建立 activation 或 tracking；显式 skill_load 仍消费冻结 catalog/source 合同

### Requirement: 普通文件访问必须保持轻量且与来源生命周期分离

列目录、搜索和普通读取 MUST NOT 隐式逐文件持久注册 ResourceIdentity、来源 revision、snapshot 或 context tracking。结果可只携带规范 URI、节点类型和所请求的内容；文件 hash 按内容读取或冲突检查需要计算，不为列目录全量预计算。显式进入上下文追踪/历史恢复时才由原 owner 建立稳定 ResourceIdentity、revision 和快照，根的注册不得被解释为所有子文件都已注册。

#### Scenario: 搜索不放大来源注册

- **WHEN** 搜索大量未追踪源码文件并返回有界结果
- **THEN** 来源注册/快照/activation 表的条目数保持不变，结果 URI 仍可访问；搜索不隐式激活 Skill

#### Scenario: 显式追踪使用原来源 owner

- **WHEN** 用户或业务显式请求追踪一个可访问文件
- **THEN** 既有来源 owner 负责身份和生命周期，普通文件工具不另建快照或第二状态机

### Requirement: gateway authority 承载稳定 gateway_id 且缺省等价本机

URI 的可选 gateway authority 段 MUST 承载**稳定 gateway_id**：段缺省表示本机 gateway；段等于本机 gateway_id 与缺省等价；段等于对端 gateway_id 表示跨 gateway。系统 MUST NOT 为「其它 gateway」引入新 scope，MUST NOT 把 authority 段与 scope 段混为一谈，MUST NOT 让 authority 承载瞬时通道标识（channel instance/epoch/route）。

#### Scenario: authority 缺省等价本机

- **WHEN** URI 不含 authority 段
- **THEN** 系统按本机 gateway 解析，且与显式写本机 gateway_id 产生相同的解析结果

#### Scenario: 对端 authority 触发跨 gateway 解析

- **WHEN** URI 的 authority 段等于一个对端 gateway_id
- **THEN** 系统进入跨 gateway 解析链，判定依据仅为该 authority 段，不依据 scope 或 kind

#### Scenario: authority 不承载瞬时通道标识

- **WHEN** 一次跨 gateway 解析经过某条具体通道
- **THEN** 通道实例/epoch/路由 locator MUST NOT 出现在 URI、持久化记录或业务幂等键中；authority 只表达稳定 gateway_id

### Requirement: URI 解析必须是星型且以 policy 常量界定上界

URI 解析 MUST 按唯一顺序执行：本地 parse（fail-closed）→ 无 authority 或 authority 等价本机时由本进程按 workspace registry 解析 → authority 指向对端时按联邦关系转发：本地 gateway 是自身联邦的 **hub** 时可直接解析其**直接 spoke** 的资源；本地 gateway 是 **spoke** 时通过其**唯一 hub** 做**一次有界 transit 解析** → 不可达、未共享或未找到时 MUST fail-closed 返回结构化拒绝码。系统 MUST NOT 回退到本地猜测路径、空路径或任何虚假默认值。

跨边界 transit MUST 携带 `visited set`、`max_transit_gateways`、`max_gateway_hops` 与**总 deadline**，且这些上界 MUST 是**显式策略常量**（集中定义），MUST NOT 硬编码为散落的魔法数字；拓扑变化时 MUST 改策略而非重写解析器。

**解析命中只返回稳定身份与内容，不返回、不携带 locator**：`locator 是输入，不是输出` 是本 capability 可机械检查的不变量。

#### Scenario: 跨 gateway 只传逻辑事实

- **WHEN** 解析请求跨过 gateway 边界
- **THEN** 边界两侧只传递 identity、URI、revision 与资源内容，MUST NOT 传递 real path、provider locator 或 credential

#### Scenario: hub 可直接解析直接 spoke

- **WHEN** 本地 gateway 是其联邦的 hub，且 authority 指向一个直接 spoke
- **THEN** 系统按直接 spoke 关系解析，不额外经过第三方中转

#### Scenario: spoke 经唯一 hub 有界 transit

- **WHEN** 本地 gateway 是 spoke，且 authority 指向一个非直接对端
- **THEN** 系统通过其唯一 hub 做一次有界 transit 解析，并携带 `visited set`、`max_transit_gateways=1`、`max_gateway_hops=2` 与总 deadline；MUST NOT 继续递归转发到第四个 gateway

#### Scenario: locator 不是输出

- **WHEN** 一次解析（含跨 gateway 命中）成功返回
- **THEN** 其返回值只含稳定身份与内容，不含任何 locator；locator 仅作为本次解析的输入存在

#### Scenario: 超过上界显式失败

- **WHEN** 解析需要的中继次数或跳数超过 policy 常量，或总 deadline 耗尽
- **THEN** 系统 fail-closed 返回联邦解析期闭集内的结构化拒绝码（`federation-transit-limit-exceeded` / `federation-deadline-exceeded`），MUST NOT 以本地同名资源、空结果或缓存猜值替代

#### Scenario: 对端不可达时显式失败

- **WHEN** authority 指向的对端 gateway 不可达
- **THEN** 系统返回联邦解析期闭集内**不可达**的结构化拒绝码（`federation-channel-closed`），MUST NOT 用本地同名资源、空结果或缓存猜值替代

#### Scenario: 未共享时显式失败

- **WHEN** 对端 gateway 可达但未向本机共享目标资源
- **THEN** 系统返回联邦解析期闭集内**未共享 / 不可解析**的结构化拒绝码（`target_not_resolvable`），对「未授权存在」与「不存在」返回同一结果，不泄露 locator

#### Scenario: 未知 gateway 显式失败

- **WHEN** authority 段指向本机未登记的 gateway_id
- **THEN** 系统返回联邦解析期闭集内**未知 / 未登记对端**的结构化拒绝码（`federation-unknown-peer`），且不尝试按名称猜测路由

### Requirement: 拒绝码必须分三套集中登记且命名不得自造

系统 MUST 复用既有拒绝码命名空间与风格，并 MUST 在**唯一一处集中登记处**登记全部寻址阶段拒绝码，且 MUST **分三套独立列出（语法期 / 解析授权期 / 联邦解析期）、标明各自适用范围与「不可混用」**：

- **grammar 拒绝码（修订闭集18个）**：`ResourceUriGrammarError.reason_code`，现行17码快照位于 `grammar.py:25-44`，本次修订须在唯一 grammar owner 同步替换并新增编码校验码（`empty_uri`、`unknown_scheme`、`scheme_case_error`、`userinfo_rejected`、`query_rejected`、`fragment_rejected`、`backslash_rejected`、`control_char_rejected`、`invalid_percent_encoding`、`invalid_path_encoding`、`non_ascii_rejected`、`empty_segment`、`dot_segment`、`invalid_character`、`case_error`、`unknown_scope`、`unknown_resource_kind`、`malformed_path`）。构造函数对未登记 code 直接 `raise ValueError`（`grammar.py:51-53`），未登记码构造必须失败；适用于字符串→`ParsedResourceUri` 的语法解析期。`invalid_percent_encoding` 拒绝身份段 escape 或非法文件 escape，`invalid_path_encoding` 拒绝文件段无效 UTF-8；`non_ascii_rejected` 仍用于未编码 URI 本体，不拒绝合法编码文件名。
- **resolve 拒绝码（规范闭集 7 个）**：`ResourceUriResolveError.reason_code` 适用于已解析出 URI 之后的解析/授权期，闭集为 `scope_mismatch`、`unknown_resource`、`unknown_operation`、`capability_denied`、`snapshot_unavailable`、`historical_snapshot_missing` 与 `unsupported_view`。后者专用于已识别的 view 与资源 kind 不兼容。当前生产 resolver 已登记七码并在构造时拒绝未知码；任务 1.3 的整体状态还包含其它阶段登记验收，不据本子项单独勾选。
- **联邦解析期拒绝码（第三套，独立闭集）**：`app/gateway/federation/errors.py` 的 `FederationError.code`，**归属 `app/gateway/federation/`**，与上述 grammar 修订闭集 + resolve 7 两套不混用。真实码名（实测，含两种命名风格）：`federation-unknown-peer`（未知 / 未登记对端 gateway）、`federation-channel-closed`（channel / 对端连接不可用，即不可达）、`target_not_resolvable`（目标在当前有界拓扑内不可解析，覆盖「未共享 / 未找到」，对「未授权存在」与「不存在」返回同一码）、`target_ambiguous`（裸 session id 命中多个已授权候选）、`federation-deadline-exceeded`（总 deadline 耗尽）、`federation-transit-limit-exceeded`（中继次数 / 跳数超 policy 上界）。注意：`federation-` 前缀**不是本套一致约定**——`target_not_resolvable` 与 `target_ambiguous` 逐字不带前缀。

文件内容冲突、补丁失败及执行未知结果属于文件操作 owner 的执行合同，不冒充寻址阶段错误；该合同见 `add-unified-resource-file-tools`。上述三套寻址闭集 MUST NOT 混用：语法期 MUST NOT 抛 resolve 码或联邦解析期码，解析/授权期 MUST NOT 抛 grammar 码或联邦解析期码，联邦解析期 MUST NOT 抛 grammar 码或 resolve 码。新增码 MUST 只出现在集中登记处一次，其它模块与其它 change MUST 只引用、MUST NOT 自造同义码。

联邦完整协议闭集由 `app/gateway/federation/errors.py` 的 `FEDERATION_ERROR_CODES` 唯一登记，`FederationError` MUST 在构造时拒绝未登记码。目标 owner 已识别的资源解析、上下文 revision 或 communication observation 拒绝，跨联邦传输时外层 MUST 使用此处登记的 `federation-target-owner-rejected`，detail 恰为 `{owner, code}`，owner 分别为 `resource_uri_resolver`、`context_revision` 或 `communication_observation`；内层 code 仍归原 owner 管理，不加入联邦闭集。业务消费者 MUST 还原其原 owner 的拒绝语义，不以外层联邦码替代业务错误。对端 error frame 含未知联邦码时，接收边界 MUST 报 `federation-malformed-frame`、保留原异常原因并以协议错误关闭 channel，不将未知码伪装成正常断线。

#### Scenario: 三套拒绝码分别登记且不混用

- **WHEN** 需要引用一个拒绝码
- **THEN** 调用方按所处阶段（语法解析 / 解析授权 / 联邦解析）选择对应闭集，MUST NOT 跨集使用，且 MUST 从集中登记处引用

#### Scenario: 未登记拒绝码被视为缺陷

- **WHEN** 某实现抛出一个不在对应闭集内的拒绝码
- **THEN** 构造该错误时即显式失败（现有构造函数已对未登记 code 抛 `ValueError`），不允许闭集被静默扩展

#### Scenario: 目标 owner 拒绝跨端传输后仍保留业务归属

- **WHEN** 目标工作区以其已登记的资源解析、revision 或 observation 错误拒绝请求
- **THEN** 联邦外层使用 `federation-target-owner-rejected`，detail 只携带原 owner 和 code；业务消费者还原原拒绝，联邦构造不接受该业务码

#### Scenario: 未知联邦 wire 错误码显式终止协议

- **WHEN** 接收的 error frame 使用不在唯一联邦闭集中的 code
- **THEN** 接收边界报 `federation-malformed-frame`，保留构造失败原因并按协议错误关闭 channel

#### Scenario: 其它 change 不得自造拒绝码

- **WHEN** 另一个 change 需要表达一种新的拒绝
- **THEN** 它 MUST 引用集中登记处并说明归属哪一套闭集，MUST NOT 自行发明名称或定义同义码

#### Scenario: view 与资源 kind 不兼容使用专用 resolve 拒绝码

- **WHEN** 已识别的 view 与资源 kind 不兼容
- **THEN** resolve 阶段以 `unsupported_view` 显式失败；该码只在本登记处定义，其它 change 只引用，不得另造同义拒绝码

#### Scenario: 联邦解析期拒绝码引用真实码名

- **WHEN** 一个 scenario 描述跨 gateway 解析失败（未知 / 未登记对端、未共享、不可达、上界或 deadline 超限）
- **THEN** 它 MUST 引用联邦解析期闭集内的真实码名（`federation-unknown-peer`、`target_not_resolvable`、`federation-channel-closed`、`target_ambiguous`、`federation-deadline-exceeded`、`federation-transit-limit-exceeded`），MUST NOT 自造或改写码名

### Requirement: identity 必须独立于 URI 且不跨 scope 混同

ResourceIdentity MUST 不透明、稳定且 revision-free。同一逻辑名出现在两个不同 scope（例如 `user` 与某个 `workspace`）时 MUST 是两个不同 identity。跨来源的等价与覆盖 MUST 是独立 concern，MUST NOT 塞进 identity 或 URI 语义。

#### Scenario: 同名跨 scope 是两个 identity

- **WHEN** 同一逻辑名同时存在于 `user` scope 与某个 `workspace` scope
- **THEN** 系统为两者分配不同 ResourceIdentity，且任一方的解析不因另一方存在而改变

#### Scenario: 跨来源覆盖不改变 identity

- **WHEN** 高优先级来源覆盖低优先级来源的同名资源
- **THEN** 覆盖只影响后续 URI 解析结果与 catalog 快照，既有 identity 与既有已封存绑定保持稳定

### Requirement: 默认寻址政策必须以 URI 为默认形式

软件内部的配置、skill、状态与资源引用 MUST 默认以 URI 传递；real path MUST 只在最后访问点出现。任何新增持久化字段若需定位资源，MUST 使用 `identity + URI(+ 独立 revision 字段)` 组合，MUST NOT 存储 real path。 本政策按本 capability 具名登记的根绑定、Gateway 原始诊断日志和 Browser 本地文件页面 URL requirements 划界，例外不构成普通资源引用。

该寻址义务只适用于需要定位资源的引用；内部语义聚合是否拥有可访问地址，MUST 按本 capability 的 requirement「内部语义聚合资源不得伪造 URI」处理，不得把稳定身份误当可访问地址。

**来源控制与activation迁移 MUST 表达为必要字段、新写入与读侧切换**，由各自唯一owner核对当前schema与真实producer，不以URI仍未落盘为准入条件。旧开发中间数据按已授权策略清理，MUST NOT 为其增加扫描升级、兼容双读或URI alias。新的URI持久化仍遵守本requirement的身份/地址/位置边界；附件和工具结果旧scheme的生成、读取及投影按 `add-unified-resource-file-tools` 的消费合同完整迁移，不因此扫描旧业务数据。

#### Scenario: 新增持久化字段不得存 real path

- **WHEN** 一个 change 或实现需要新增一个用于定位资源的持久化字段
- **THEN** 它存 identity 与 URI（必要时加独立 revision 字段），不存 real path、不存 provider locator

#### Scenario: 模型可见载荷只带 URI

- **WHEN** 可寻址资源引用进入模型可见的 prompt、工具结果或历史投影
- **THEN** 它们携带 URI 与独立 revision 标识，MUST NOT 携带 real path 或 credential

#### Scenario: 迁移是新写字段

- **WHEN** 某个既有持久化字段需要承载资源引用
- **THEN** 它以 identity + URI 的新格式新写入并切换读路径，旧写入形态物理下线，且不构造任何存量扫描或数据改写

### Requirement: 内部语义聚合资源不得伪造 URI

URI SHALL 只表达由资源 owner 公布模板与访问合同的真实可寻址目标。只用于 Registry 发布和 activation 冻结的内部语义聚合，如果没有这种目标，MUST 保留自身稳定 resource identity、facet、revision、payload 和 lineage，并把 typed `display_uri` 显式保存为 NULL；该字段仍是必填字段，空字符串、缺字段和伪造地址均不得代替 NULL。当前 MCP catalog 聚合与 Skill catalog 聚合属于此类；可访问 Skill 条目和 AGENTS 来源仍必须保留真实 URI。MUST NOT 为内部聚合新增同义 kind、伪装成某个 Skill 文件，或把 resource_id 当作可解析 URI。

无 URI 是该资源 owner 在发布前明确选择的表示，不是解析失败、来源不可用、未知 kind、失权或缺失字段的兜底。每个非 NULL URI MUST 经唯一构造/grammar 与相应 owner binding 校验，非法输入 MUST 显式失败，MUST NOT 转为 NULL。消费者不得把 NULL 转成当前工作区、真实路径、空 URI 或某个默认资源。

这一合同 MUST 在同一完整闭包迁移 descriptor/snapshot、Registry、provenance/serde、activation schema/writer/reader、clone/fork/detail manifest 以及必要的 DTO/协议/投影和 fixture；不增加第二 writer、迁移旧开发数据或兼容双读。NULL 按既有 canonical JSON 规则作为 JSON null 参与原有字段的完整性/hash，不省略字段、不另设 hash 算法。sealed restore/retry/fork MUST 保留原 NULL 与绑定身份，不能根据当前 catalog 补造地址。无 URI 不改变可见性和受保护正文规则，也不让内部 identity 成为普通模型/history 的额外展示字段。

#### Scenario: 内部 catalog 没有可访问地址

- **WHEN** MCP 或 Skill catalog 聚合发布并进入 activation freeze、seal、restart restore 或 fork
- **THEN** 其 display_uri 为 NULL，原 resource identity、facet、revision、payload、lineage 与成员语义按原合同保留；Skill 条目的 URI 仍可定位原合法资源，不生成 catalog 的伪地址

#### Scenario: 非法地址不能降级为无地址

- **WHEN** producer 为有可寻址目标的资源提供 grammar 不合法或与 owner binding 不一致的 URI
- **THEN** 发布或请求显式失败，不把该值替换为 NULL，也不改投默认或当前激活资源

#### Scenario: 无地址保持封存与投影边界

- **WHEN** 无 URI 的内部聚合被持久化、恢复或投影
- **THEN** 必填 display_uri 字段保留 NULL，完整性仍覆盖该字段；不从当前来源补地址，不泄露 locator、内部身份或受保护正文，不把缺地址误报为已可导航

### Requirement: 配置来源 URI 必须使用 config kind 且只标识来源文件

配置来源的 URI MUST 使用闭集内的 `config` kind，且只标识**可寻址的来源文件本身**。`layer` MUST 作为独立兄弟字段保留，遵循本 capability 的 requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」，MUST NOT 塞进 URI。

**`inline` 来源有 URI**：它是发行包内真实存在的 JSONC 文件（`configs/workspace_inline.jsonc` / `configs/gateway_inline.jsonc`，经 `resolve_config_resource_source` 的 `is_file()` 校验），有稳定 disk 载体。

**config URI 的尾段形态（定稿）**：尾段 MUST 取该来源的**逻辑资源名**，MUST NOT 取原始文件名。尾段 MUST 是**单段**、MUST 落在动态段闭合 charset `[A-Za-z0-9_-]` 内、MUST NOT 含点号——真实文件名 `workspace_inline.jsonc` / `gateway_inline.jsonc` 的点号与扩展名不可原样入 URI（`parse_resource_uri` 对含 `.` 的尾段以 `invalid_character` fail-closed 拒绝，该 charset MUST NOT 放宽、MUST NOT 新增转义后门）。每条 `ConfigSource` 的 URI MUST 指向该记录的逻辑来源文件，并与其 `layer` 兄弟字段保持来源一致（`layer` 仍留在 URI 之外，不进 URI 字符串）；这不是 layer 与尾段的全局一对一映射，同一 `inline` layer 可以分别指向 `workspace_inline` 与 `gateway_inline`。规范形态即 `boxteam://{scope}/{scope_id}/resources/config/{logical_source_name}`，其中 `scope`/`scope_id` 按本 capability 的 scope 闭集 requirement 取值（`inline` 层取 `scope=inline` 与真实 `distribution_id`），逻辑资源名取该层可寻址载体的逻辑名（如 `workspace_inline` / `gateway_inline`）。`ConfigSourcesDTO.schema_path` 等 schema 位置字段表示独立配置文件资源地址，按同一 `config` URI 规则定位 `workspace_schema` / `gateway_schema`，不因此创建 `sources[]` 条目或伪造 layer。

没有稳定、可寻址来源文件的配置来源 MAY 不带 URI；`runtime_override` 在没有此类文件时 MAY 以 `uri = null` 表达。此时配置来源身份仍独立于 URI，MUST 遵循 requirement「identity 必须独立于 URI 且不跨 scope 混同」与「默认寻址政策必须以 URI 为默认形式」；`uri = null` 只表示来源元数据没有可寻址文件位置，不构成可持久化资源 locator，也不授权存储真实路径。MUST NOT 将来源身份塞入 URI 或为它编造新的 `kind` / 地址形态。真实路径 MUST 继续只在读取内容的调用栈内出现。

#### Scenario: config 资源有 URI

- **WHEN** 系统为一条 `inline` 配置来源构造地址
- **THEN** 它使用 `config` kind 的 URI，`layer` 作为兄弟字段随行，URI 字符串本身不含 layer 取值

#### Scenario: config URI 尾段是逻辑资源名而非原始文件名

- **WHEN** 系统为一条 `inline` 层配置来源构造 URI，其底层真实文件名为 `workspace_inline.jsonc`
- **THEN** 尾段取逻辑资源名（`workspace_inline`，单段、落在 `[A-Za-z0-9_-]` 内、无点号），MUST NOT 取含 `.jsonc` 的原始文件名；含点号的尾段被 grammar 以 `invalid_character` 拒绝，且 charset MUST NOT 被放宽

#### Scenario: 没有可寻址来源文件时不编造 URI

- **WHEN** 系统处理没有稳定、可寻址来源文件的 `runtime_override`
- **THEN** `uri` MAY 为空，来源仍按既有身份字段识别；系统 MUST NOT 为内部 SQLite carrier 或 runtime override 编造 URI

### Requirement: 配置 layer 必须表示逻辑来源且与载体和快照分离

config `layer` MUST 只表示逻辑配置来源，其闭集与 `precedence` 唯一由本 requirement 定义：`inline` → 0，`user` → 1，`user_local` → 2，`workspace` → 3，`runtime_override` → 4。`sqlite` 是实现可选的内部持久化 carrier，MUST NOT 作为 layer 值或新增公开的 storage/carrier 字段暴露。读侧原以 `sqlite` 表示的 runtime override MUST 改用 `runtime_override`，并保持其既有 `precedence`；其它来源的既有 precedence MUST 保持不变。

`active_snapshot` 与 `pending_snapshot` 是配置状态快照，不是配置来源，MUST NOT 作为 `sources[]` 条目或 `layer` 值出现；快照生命周期 MUST 与来源清单分开表达，MUST NOT 把它们重映射成 `runtime_override`。`source_key` 到 `layer`/`precedence` 的映射 MUST 由其配置数据 owner 唯一登记，所有读路径 MUST 使用同一映射，不得按 SQLite 等物理 carrier 推导或覆盖逻辑来源层。

#### Scenario: Workspace runtime override 的逻辑层与优先级不受物理 carrier 改写

- **WHEN** 系统读取 `workspace_runtime_override` 配置来源
- **THEN** 来源报告 `layer=runtime_override` 与既有 `precedence=4`；不因 carrier 改写成 `sqlite`，也不新增公开 storage/carrier 字段

#### Scenario: active 与 pending snapshot 不进入来源清单

- **WHEN** 系统恢复或报告 active/pending 配置快照
- **THEN** 快照保持独立的配置状态，不作为 `sources[]` 中的来源条目，不获得 config `layer` 或来源 URI

### Requirement: config 的 layer 轴与 URI 的 scope 轴相互独立且同名不蕴含同义

系统的 config `layer` 轴与 URI `scope` 轴 MUST 被当作**两个相互独立、不可互换的轴**：`layer` 的唯一规范定义见本 capability 的 requirement「配置 layer 必须表示逻辑来源且与载体和快照分离」；`scope` 的唯一规范定义见本 capability 的 requirement「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」。两个轴都含 `inline`，且另有同名异义取值——**同名 MUST NOT 被解释为同义、MUST NOT 被当作等价或可互换的枚举**；本 requirement MUST NOT 复述任一轴的取值表。

#### Scenario: 同名不同轴不被混用

- **WHEN** 实现或测试同时处理 config `layer` 与 URI `scope`
- **THEN** 两者各自按自己的轴校验与取值，MUST NOT 用其中一个轴的闭集去校验另一个轴的输入，也 MUST NOT 由 `layer` 取值直接推出 `scope` 取值

#### Scenario: 同一 source_key 在所有读路径报同一逻辑来源层

- **WHEN** 系统从源 JSONC 构建或从 active snapshot 的来源基线恢复同一条配置来源，并取得其 `layer` 与 `precedence`
- **THEN** 对同一 `source_key` 的所有读路径 MUST 返回同一逻辑来源层与同一优先级；结果 MUST 符合本 capability 的 config layer 权威表，不得按物理 carrier 改写，也不得把 active/pending snapshot 本身报告成来源

### Requirement: 既有配置来源持久化必须按同一模式迁移为 URI 兄弟字段

系统 MUST 消除 real path 持久化违约：配置来源层记录（`app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord`/`ConfigSourceJournalRecord`）MUST NOT 携带 `source_path`/`backup_path` 一类真实路径字段；配置来源列表 API 响应体（`GET /api/v1/config/sources`，经 `app/api/config.py` 的 `ConfigSourceDTO.path` 与 `ConfigSourcesDTO.schema_path`）MUST NOT 输出真实路径。**该违约已由 `50bffa45`（持久化侧：`source_path`/`backup_path` 物理删除、改 `uri: str | None`）与 `76ed0089`（响应体侧：`path` 与 `schema_path` 均改配置来源 URI）消除**；本节保留为可机械复核的不变量，实施期若回退即缺陷。

迁移 MUST **直接复用 config 侧既有的平级属性模式**（`app/core/config_sources.py:16` 的 `ConfigSource` 已是 `path` + `layer` + `precedence` 平级，且 `layer_revision`/`layer_digest`/`source_generation` 已是兄弟字段），即「把 `path` 换成 `uri`，其余 sibling 字段原样保留」。MUST NOT 另发明第二套表示。

#### Scenario: 配置来源不再持久化 real path

- **WHEN** 一条配置来源被写入持久化记录
- **THEN** 记录中承载 URI 而非 real path；real path 只在读取该来源内容时于调用栈内出现

#### Scenario: API 不输出配置来源真实路径

- **WHEN** 客户端请求配置来源列表（`GET /api/v1/config/sources`）
- **THEN** 响应体只含 URI 与兄弟字段（layer/precedence/layer_revision/layer_digest/source_generation 等），不含真实路径；响应体的 **每一个** 承载来源位置的字段（含 `ConfigSourceDTO.path` 与 `ConfigSourcesDTO.schema_path`）都 MUST 是 URI，MUST NOT 存在承载 real path 的字段

#### Scenario: 复用既有平级属性模式

- **WHEN** 实现配置来源的 URI 化
- **THEN** 它沿用既有 `path`→`uri` 的平级属性替换，其余 sibling 字段（layer、precedence、revision、digest、generation）原样保留，不新增第二套结构

### Requirement: 多工作区场景下寻址层必须显式承载 scope_id 身份

**归属与引用**：本 requirement 的多工作区前置条件由 `add-multi-workspace-backend-mounting` 承载；其 requirement `进程内必须维护权威的已挂载工作区注册表`、`workspace 身份必须由显式寻址载体承载`、`workspace_id 必须与 URI workspace scope 使用同一份身份`、`持久化数据不得以「当前激活工作区」为前提` 是本 change 多工作区部分的引用来源。本 change 只声明寻址层要求，不复制其实现要求。本 capability 的 scope_id 取值表 MUST 与 `add-multi-workspace-backend-mounting` 的挂载模型保持一致，MUST NOT 各自定义 scope_id 取值语义。

在一个后端进程可挂载多个工作区的前提下，scope_id 身份 MUST 在寻址层显式表达（HTTP API 与 URI 皆然）。系统 MUST NOT 依赖「当前激活工作区」作为持久化数据的前提。

#### Scenario: 单进程多工作区各自显式寻址

- **WHEN** 同一后端进程同时挂载多个工作区
- **THEN** 每条对工作区资源的 URI 都在 `scope_id` 段显式携带对应 workspace_id，解析结果不随「当前激活工作区」切换而改变

#### Scenario: 持久化数据不绑定激活态

- **WHEN** 一条持久化记录引用工作区资源
- **THEN** 其含义只由记录内显式的 scope/scope_id 与 URI 决定，与记录写入时或读取时的「当前激活工作区」无关

### Requirement: 正名 builtin 到 inline 与 layer 改名必须同步且不得误改无关同名

系统 MUST 把 URI scope 由 `builtin` 正名为 `inline`，并 MUST 把 skill catalog 的 `layer` 名同步由 `bundled` 正名为 `inline`（理由：同一概念三个名字——layer `bundled`、scope `builtin`、config layer `inline`——正是本仓库要求根除的）。两处都改名后，`app/agents/skill_runtime.py` 的映射 shim 退化为恒等映射并**已被 `298ef599` 物理删除**（`layer_order` 改为 `(inline, gateway, workspace)`，`scope = {...}[layer]` 改名 shim 已不存在）。

改名 MUST 带影响评估结论并据此定级；`layer` 进入 entry identity 与 catalog payload，scope/layer 的统一是契约调整，不构造旧数据扫描或迁移。历史“display_uri writer 生产未调用”只描述当时基线，不能作为实施前置；当前 activation 新写入和恢复消费必须随真实调用闭包迁移，并遵守本 capability 的新写字段与内部聚合无 URI 合同。

**真依赖 URI scope `builtin` 的位置**：`grammar.py` 现**已无 `builtin` 字面量**（`rg 'builtin' grammar.py` 零命中），scope 闭集 `_SCOPE_KEYWORDS`（`grammar.py:18`）已是 `{workspace, user, gateway, inline}`；改名的实际约束落在 `resolver.py` 的 scope 构造校验与 `require_scope_binding`（闭集映射已含 `inline`）。其余 `builtin` 命中（工具 `origin="builtin"`、主题来源、`builtin_tool_registry` 等）是无关同名，MUST NOT 误改。

#### Scenario: scope 与 layer 同步正名

- **WHEN** 系统产出 Skill 来源标识
- **THEN** scope 使用 `inline`、layer 使用 `inline`，MUST NOT 继续产出 `builtin` scope 或 `bundled` layer，且不保留运行时别名

#### Scenario: 改名不构造存量迁移

- **WHEN** 实施 scope/layer 正名
- **THEN** 同步真实 producer 与新写入/读侧合同，MUST NOT 扫描或改写旧开发持久化记录，也不以是否已有生产写入决定是否保留双轨

#### Scenario: 无关同名不被误改

- **WHEN** 实施改名时检索 `builtin`
- **THEN** 只改真实依赖 URI scope 的位置（`grammar.py:18` 的 scope 闭集 `_SCOPE_KEYWORDS` 与 `resolver.py` 的 scope 构造校验；`grammar.py` 已无 `builtin` 字面量、`_SKILL_SCOPES` 已不存在），工具 origin、主题来源与 `builtin_tool_registry` 等无关同名保持不变

### Requirement: Session 集合与 Thread 创建目标必须有正式领域 URI

统一寻址 owner SHALL 在既有 `session` kind 登记 Workspace Session 集合根 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/session`、单 Session `.../resources/session/{session_id}` 和精确 Thread `.../resources/session/{session_id}/threads/{thread_id}`。集合根、Session和Thread均为domain_object，尾段仅按登记的领域模板校验，不允许猜测其私有文件树。集合根只表示工作区会话容器，不以虚构Session ID占位；导航父节点或parent thread以并列结构化引用传递。

URI的位置、revision/view/anchor的并列字段及ResourceIdentity职责 MUST 保持原合同。集合、Session和Thread分别从权威workspace/catalog/thread owner取得身份和允许操作；Thread ID必须属于URI中的Session。目标创建使用容器URI与 `new_session_main|new_child_thread` 意图，创建完成返回真实新Thread URI；不能将未创建对象的猜测地址作为已存在资源。对 fork 的 `new_child_thread`，目标 URI 若含已存在 Thread ID，只校验并定位该 Thread 所属 Session；它 MUST NOT 建立新 Thread 的 parent-thread 关系。来源 Thread 仍由独立 source URI 固定，source lineage 与目标导航/Thread parent 关系分离；不得从 source 或 target URI 推断 parent-thread。

已有 send/read/wait 的 Session-main 产品入口不因Thread URI登记而自动扩展到child或获得fork能力；具体操作授权仍归对应owner。所有模板构造、解析及跨端路由 MUST 迁齐后删除fork专属URI拼接/解析，不提供旧字符串alias。

#### Scenario: 容器和已存在 Thread 定位不同

- **WHEN** 用户选择工作区新Session main、已有Session的新child或一个已存在的source child
- **THEN** 分别使用集合根、Session容器及完整Thread URI；集合根不制造Session ID，Thread必须由catalog核归属，创建后返回真实目标URI

#### Scenario: Thread URI 不开启文件导航或通信权限

- **WHEN** 对Thread URI追加猜测的SQLite路径，或拿已有通信grant请求fork
- **THEN** owner按模板/操作授权拒绝，不打开私有存储、不把地址可解析视为执行授权

### Requirement: 资源目标处理必须复用 owner 事实并单点决策

资源引用进入新的Session/Thread/Workspace时，系统 SHALL 通过同一个资源目标检查入口解析source/target并请求实际领域owner的权威事实，按继承意图、目标可用性、固定修订、生命周期和实际操作能力选择 `reuse_reference|retain_reference|copy_snapshot|omit|unsupported`。fork、侧边会话和相关资源消费者只消费该决定；附件、工具输出、detail、技能来源、调试方案及工作区资源不得各自按fork模式维护另一复制矩阵。

判断 MUST 优先复用已有ResourceIdentity/业务ID、URI解析范围、workspace_id、owner/来源及thread关联、retention_scope、revision/hash/length、availability/status和owner已公布operations。URI的workspace scope只表示寻址范围，不能推出workspace保留或共享权限；相同hash不能证明目标可访问；同一后端经不同Gateway挂载时，是否同一权威存储域由实际owner核实，不按authority字符串、端口或真实路径猜测。固定修订需要明确核验，不能用目标当前最新文件替代历史版本。

同存储域可读且生命周期独立的共享资源 MUST 复用已有身份和正文，不额外复制或重造内容身份。正文共享但仍依赖source owner保活时，只建立必要目标引用/retention；生命周期事实继续由原owner持久维护。目标已拥有相同已校验内容/修订时复用它，否则只有显式选择且owner支持导出/导入时才copy snapshot。未选择额外资源时不传输跨域正文，并明确omit/unavailable原因。终端、浏览器等运行实例不进行磁盘复制或自动恢复；显式关联仍遵守其owner的scope/lease限制。

缺少的能力 SHALL 在资源owner的既有operation/capability合同中集中声明，例如目标引用保留、冻结导出和快照导入；现有字段能推导的scope、不可变性和生命周期不重复落库，不扩充ResourceIdentity、不在每个实例增加fork_copy/shareable布尔字段。检查结果可以保存本次操作所需revision/preimage/回执，但不形成第二个资源状态或生命周期registry；声明支持不能代替owner实际完成操作和校验。

#### Scenario: 共享附件跳过字节复制

- **WHEN** 同工作区新thread引用已有不可变blob，owner确认目标可访问并具有独立生命周期或可建立保活
- **THEN** 决定为reuse_reference或retain_reference，正文和内容身份复用；只有需要时登记目标保活，不重复hash整个blob、不生成多余snapshot或copy claim

#### Scenario: 可变资源不能用最新版冒充冻结历史

- **WHEN** fork请求某固定revision，但同工作区文件或技能已变化
- **THEN** owner复用所需固定snapshot或按能力处理，所需revision不可用时明确失败，不以最新版补旧正文、不因同工作区跳过修订检查

#### Scenario: 跨域资源复用目标已有内容

- **WHEN** 用户显式继承资源，目标owner已拥有匹配内容及修订并核验可访问
- **THEN** 直接绑定目标已有资源，仅处理必要引用；没有匹配且支持export/import时流式导入，不支持或未选择时明确返回原因

#### Scenario: 保留范围与操作能力来自 owner

- **WHEN** thread范围资源或未知状态资源被请求复用到另一个thread
- **THEN** 通用入口不能自动提升scope、伪造可用状态或绕过lease；实际owner拒绝或按已授权的操作完成后返回权威结果
