## Purpose

为软件内部与模型可见载荷中的资源引用建立**唯一**的寻址抽象与词汇：严格区分资源身份（ResourceIdentity）、虚拟资源地址（VRN）与真实路径（real path），把 `workspace`、`user`、`gateway`、`inline` 等作用域与其它工作区、其它 gateway 收敛到同一套寻址，并以顶层 gateway 之间的**星型解析**完成跨边界定位。本 capability 是这套抽象、scope 闭集、scope_id 语义表、VRN 语法、kind 闭集与拒绝码登记处的唯一 owner。

**契约版本**：本 capability 采用**契约修正 v2**（保留既有段序，`resources` 为保留固定段，`scope_id` 对**所有** scope 都必填），并已依据**仓库内受版本控制的事实源**（`app/services/infrastructure/resource_platform/virtual_resources/grammar.py`、`resolver.py`、`values.py` 三处逐字取值，关键取值已内联在本 spec 各处；不引用任何 `out/tests/temp/**` 临时产物）**定稿** scope 闭集、scope_id 语义、kind 闭集与拒绝码登记。`memory` 已确证**不是 VRN scope**，MUST NOT 出现在闭集内。

## ADDED Requirements

### Requirement: 三层职责必须严格分离

系统 MUST 把资源引用区分为三层，每层有唯一 owner 且职责不可互换：

- **资源身份 / ResourceIdentity**：不透明、稳定、**不含 revision**、**不依赖当前激活工作区**，持久化，用于去重与 lineage。
- **虚拟资源地址 / VRN**：可解析的地址，持久化，**允许悬空**（指向已不存在的资源是合法值），**禁止编码 revision 或 hash**，是软件内部与模型可见载荷中传递资源的默认形式。
- **真实路径 / real path**：机器本地、**临时**、**永不持久化**、**永不进入模型可见载荷**、**永不跨 gateway 边界**，只作为 fs/sqlite 调用点内的局部变量存在。

#### Scenario: real path 只作为局部变量存在

- **WHEN** 某个 owner 需要访问底层文件或 sqlite 资源
- **THEN** 它先由 identity + VRN 解析出 real path，且该 real path 只在该调用栈内使用，不写入任何持久化记录、不写入 API 响应体、不写入模型可见载荷

#### Scenario: real path 外泄即缺陷

- **WHEN** 一次 API 响应体、一条持久化记录或一份模型可见载荷中出现了 real path
- **THEN** 系统 MUST 将其判定为缺陷并显式失败，不得以 `display_uri`、日志脱敏或截断静默掩盖

#### Scenario: VRN 允许悬空

- **WHEN** 一条持久化的 VRN 指向的资源配置已被删除
- **THEN** 该 VRN 仍是合法值，读取时返回结构化「未找到」拒绝码，而不是把 VRN 判定为格式非法

#### Scenario: revision 不进入 VRN

- **WHEN** 系统需要表达某资源的精确 revision 或 hash
- **THEN** 它使用与 VRN 并列的独立 revision/hash 字段，绝不把 revision/hash 编码进 VRN 字符串

#### Scenario: identity 不承担寻址职责

- **WHEN** 调用方持有 ResourceIdentity 但需要访问资源
- **THEN** 它仍然通过 VRN（或 VRN 解析链）定位资源，不得把 identity 当作可解析地址；反之 VRN 也不得被当作资源身份用于去重

### Requirement: 「位置」必须有可机械判定的统一判据

资源引用中的「位置 / location」判定 MUST 使用如下**可机械判定**的统一判据，MUST NOT 各 change 自行解释：一个字段承载的是「位置」，当且仅当它（或由它经确定性推导得到的值）可直接用于 `open`/`stat`/`join` 等文件系统或 sqlite 访问，且指向本体位于持久化记录 owner 之外的资源或产物。据此：

- **可判定为「位置」⇒ MUST NOT 落盘**：任何「位置」MUST 由 `资源身份 / ResourceIdentity` 加 `虚拟资源地址 / VRN`（必要时加并列 revision 字段）表达；当且仅当该位置可由 owner 身份（资源 id 与工作区身份）确定性推导时，MUST NOT 落盘，改为在该次 fs/进程调用栈内由 owner 身份重推导。
- **不可由 owner 身份推导、但必须对外可寻址 ⇒ MUST 用既有 API 端点引用**，MUST NOT 落盘 real path、MUST NOT 为此自造 VRN kind、MUST NOT 裸拼接 VRN。
- **唯一例外边界**：owner **自身**的运行态字段（典型为终端 shell 的当前工作目录）表达的是 owner 的运行状态而非「资源所在位置」，MUST NOT 被当作位置引用或位置表达；此类字段 MUST 以工作区内**相对路径**持久化，MUST NOT 落盘绝对路径或工作区根路径。
- **real path 出现在持久化记录、API 响应体或模型可见载荷中 ⇒ 缺陷**：MUST 显式失败，MUST NOT 以脱敏、截断或 `display_uri` 静默掩盖。

以上判据 MUST 是「位置 vs 非位置」的唯一判定来源；`add-workspace-persistent-resource-management` 的 D-A2/D-A3 与 owner 运行态字段例外 MUST 引用本 requirement，MUST NOT 另立一套判据。

#### Scenario: 可由 owner 身份推导的位置不落盘

- **WHEN** 一条持久化记录原本需要表达一个可由 owner 身份（资源 id 与工作区身份）确定性推导的资源或产物位置
- **THEN** 该位置 MUST NOT 进入持久化记录，改为在该次调用栈内由 owner 身份重推导；MUST NOT 写入绝对路径或等价的文件系统路径

#### Scenario: 不可推导但需寻址时用既有 API 端点

- **WHEN** 一条持久化记录或模型可见载荷需要对外寻址一个无法由 owner 身份推导的产物
- **THEN** 可寻址性 MUST 由承载该能力的既有后端 API 端点引用提供；MUST NOT 自造 VRN kind、MUST NOT 裸拼接 VRN、MUST NOT 暴露文件系统路径

#### Scenario: owner 自身运行态字段以工作区内相对路径持久化

- **WHEN** 持久化记录需要保存 owner 自身的运行态字段（典型为终端 shell 的当前工作目录）
- **THEN** 该字段 MUST 以工作区内相对路径持久化，MUST NOT 落盘绝对路径或工作区根路径；该字段 MUST NOT 被当作「资源所在位置」的位置引用，也 MUST NOT 被扩张解释为「任何位置都可改用文件系统路径代替 VRN」

#### Scenario: 判据可机械判定

- **WHEN** 审查一个字段是否属「位置」
- **THEN** 判定只依赖「能否经确定性推导直接用于文件系统 / sqlite 访问」与「是否属于 owner 自身运行态字段」两项可机械检查的事实，MUST NOT 依赖调用方解释或领域术语

#### Scenario: 配置应用事件的路径字段不是「位置」

- **WHEN** 配置应用事件与配置来源响应报告 `changed_paths` / `applied_paths` / `deferred_paths`
- **THEN** 这些值的口径 MUST 是配置 JSON 的 **JSON Pointer**（以 `/` 起的分段键路径，由 `app/services/infrastructure/config/state.py` 的 `changed_json_paths` 单点产出），表达「哪一项配置键变了」，MUST NOT 是文件系统路径或工作区 real path；故它们按本 requirement 判据**不属「位置」**，不适用 VRN 表达与「不得落盘」义务，也 MUST NOT 被新增用途扩大解释为可承载 real path

### Requirement: scope 必须取自定稿闭集且 scope_id 对所有 scope 必填

VRN 的 scope MUST 取自**定稿闭集** `workspace` | `user` | `gateway` | `inline`（依据权威表：`builtin` 正名为 `inline`；`user` 为本次新增；`memory` 已移出）。该闭集与每个 scope 的 scope_id 取值来源 MUST 由本 capability 的**唯一一张表**规定，其它模块与 change MUST NOT 自行发明 scope 名或 scope_id 语义。该表 MUST 与 `add-multi-workspace-backend-mounting` 的挂载模型保持一致（其 `workspace` scope 的 scope_id 与显式 HTTP 寻址的 workspace_id 同源）；本 capability 与该 change MUST NOT 各自定义 scope_id 取值语义，取值规则一律以本表为唯一出处。

**`memory` 已确证不是 VRN scope，MUST NOT 出现在闭集内**：它零生产构造方、resolver 连 scope_id 都不比对、`kind="memory"` 全仓零构造、container 未装配（原 `configs/workspace_inline.jsonc:427-433` 的 `agent.memory` 6 键配置块与 schema `$defs.agentMemory` 已随 `remove-agent-memory` 物理删除，现配置已无该块）；**`memory` 的 domain owner 与状态本体从未接入，故无 VRN 替代 owner 的需求**。解析器侧 MUST 物理移除既有两点式 `boxteam://memory/{scope}/{name}`（无 `resources` 固定段、无 kind、恰好两段）的特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝（**已由提交 32bc6256 落地**：该两点式特例分支与 `memory_display_uri` 构造函数均已物理删除）；MUST NOT 把该形态当作合法 VRN 接受或产出。

`scope_id` 段 **MUST 对所有 scope 都出现且必填**，MUST NOT 只对某个 scope 必填。`scope_id` MUST 由**真实身份推导**，MUST NOT 硬编码字面量，MUST NOT 依赖隐含上下文（依据权威表）：

- `workspace` → 真实 workspace_id（现状即为真实 id）；
- `gateway` → **真实 gateway_id**（取值来源与注入 owner MUST 按本 capability 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」定稿落地）；
- `inline` → **真实 distribution_id**（修订前现状为与 `gateway` 逐字共用字面量 `local`、`distribution_id` 全仓零生产赋值，属既有不一致；**已由 `298ef599`+`f3bd8213` 落地改为按 manifest 推导，2026-10-01 第八轮复核更正**）；来源与编码 MUST 按本 capability 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」定稿落地； **（修订注，`298ef599`+`f3bd8213` 落地）**：现状已改为按 manifest 推导，不再与 `gateway` 共用字面量 `local`，`distribution_id` 不再是全仓零生产赋值。
- `user` → `local`，并 MUST 显式声明为**单用户本地程序的约定**（AGENTS.md 明确无云服务、无多租户），MUST NOT 虚构用户名； **（修订注，`f6fc990f` 落地）**：现状已在 `grammar.py` 的 `_SCOPE_KEYWORDS` 落地 `user` scope，`user` 的 `scope_id` 由 `app/core/user_identity.py::user_scope_id` 单点返回 `local`（与 `inline` 的 `distribution_identity`、`gateway` 的真实 gateway_id 推导同族），resolve 时经 `ResolutionContext.user_scope_id` 显式携带并由 `resolver.require_scope_binding` 校验，故 `user` 不再是「parse 成功而 resolve 无绑定」的悬空 scope。

「当前工作区」不是寻址概念，MUST NOT 作为持久化数据的隐含前提。其它工作区 MUST 复用 `workspace` scope 加另一个 `workspace_id` 表达，MUST NOT 引入新 scope。

#### Scenario: scope_id 对所有 scope 必填

- **WHEN** 系统构造或解析任意 scope 的 VRN
- **THEN** 路径中 MUST 出现显式 `scope_id` 段；任何省略 `scope_id`、或依赖「当前激活工作区 / 当前 gateway / 当前发行版」补齐缺省 scope_id 的解析一律被拒绝

#### Scenario: workspace scope 的 scope_id 是真实 workspace_id

- **WHEN** 解析一个 `workspace` scope 的 VRN
- **THEN** `scope_id` 段被解释为真实 workspace_id；同 gateway 下的其它工作区就是另一个 `workspace_id`，不新增 scope

#### Scenario: gateway 与 inline 的 scope_id 由真实身份推导而非硬编码

- **WHEN** 系统为一个 `gateway` 或 `inline` scope 构造 VRN
- **THEN** `gateway` 的 `scope_id` 取真实 gateway_id（而非字面量 `local`）、`inline` 的 `scope_id` 取真实 distribution_id（而非与 gateway 共用的 `local`）；后者按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」的编码规则从 manifest 推导

#### Scenario: user scope 的 scope_id 是单用户约定值 local

- **WHEN** 系统为一个 `user` scope 构造 VRN
- **THEN** `scope_id` 为 `local`，并显式以「单用户本地程序」解释该值，不虚构用户名或细分身份

#### Scenario: inline 取代 builtin

- **WHEN** 系统需要表达发行包内置层的资源
- **THEN** 它使用 `inline` scope，并 MUST NOT 继续产出或接受 `builtin` 作为 scope 名，也不保留 `builtin`→`inline` 的运行时别名

#### Scenario: 未知 scope 被拒绝

- **WHEN** 解析遇到闭集以外的 scope 段（含 `memory`、`session`、`sqlite`）
- **THEN** 系统返回 scope 未登记的结构化拒绝码，且不尝试任何猜测映射

#### Scenario: memory 特例分支必须被物理移除并以 unknown_scope 拒绝

- **WHEN** 调用方提交 `boxteam://memory/{scope}/{name}` 一类两点式字符串
- **THEN** 解析器侧 MUST 已物理移除该两点式特例分支，并以 `unknown_scope` 类拒绝码 fail-closed 拒绝；MUST NOT 按 VRN 解释该字符串，MUST NOT 为其定义 scope_id

### Requirement: inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导

`inline` scope 的 `scope_id` MUST 由该发行包的 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）中的 `distribution` 与 `version` 两个字段**确定性推导**，MUST NOT 取自目录名、安装路径或任何随环境变化的量（否则跨 gateway 与跨机器寻址从根上不成立）。

**为什么需要编码**：VRN 动态段的闭合 charset 为 `[A-Za-z0-9_-]`（`grammar.py:23` 的 `_NAME_CHARSET`），MUST NOT 放宽。而 `version` 的实测形态是语义化版本（发行包 `version` 取自根 `package.json` 的 `version`，实测为 `0.0.2`），**含点号 `.`，不在 charset 内** —— 直接拼接（如 `source-development-0.0.2`）会被 grammar 以「含未登记字符」结构化拒绝。因此 MUST 在 charset 内选择编码，MUST NOT 放宽 charset 或新增转义后门。

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

- **WHEN** 系统为 `inline` scope 构造 VRN
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

- **WHEN** Gateway 代理一个 `/api/v1/*` 请求给 workspace 后端，且 workspace 后端为 `gateway` scope 构造 VRN
- **THEN** Gateway MUST 附加 `X-BoxTeam-Gateway-Id`；后端 MUST 从请求上下文读入该值并作为 `scope_id`（即 `identity.json` 里的真实 gateway_id），MUST NOT 取 host:port、监听端口或字面量 `local`

#### Scenario: gateway 身份缺失或非法时 fail-closed

- **WHEN** 请求未携带 Gateway 身份头，或头值不是合法 gateway_id
- **THEN** 系统 MUST fail-closed 显式拒绝，MUST NOT 回退 `local`、MUST NOT 用进程级单例或「当前激活」态补齐

#### Scenario: gateway 身份按请求注入而非进程级单例

- **WHEN** 同一 workspace 后端进程被两个不同 Gateway 挂载并分别收到请求
- **THEN** 两次请求各自读入**自己** Gateway 注入的身份并得到各自的 `gateway_id`，MUST NOT 命中任何进程级「当前 gateway」单例

### Requirement: VRN 语法必须保留固定段序并单一实现

统一 VRN 语法 MUST 为：

```text
boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}
```

其中 `{gateway_authority?}` 为**可选单段**，承载**稳定 gateway_id**，缺省即本机 gateway；`resources` 是**固定保留段**（MUST NOT 省略、MUST NOT 被简化掉）；`{kind}` 取自**定稿闭集**（见下一条 requirement）。系统 MUST 使用闭合 charset（动段 `[A-Za-z0-9_-]`，`grammar.py:23`）；MUST 拒绝百分号编码与 `#fragment`（均在分段之前整体拒绝，`grammar.py:153`/`:160`，故不存在二次解码歧义）；大小写**不折叠**（变体一律结构化拒绝），分隔符唯一为 `/`（`\` 整体拒绝），相对段 `.`/`..` 显式拒绝；规范化 MUST 只有单一实现。现有 skill 形态 `boxteam://workspace/{workspace_id}/resources/skills/{name}/SKILL.md` MUST 是本语法的特例（authority 缺省），MUST NOT 存在第二套并列语法。 **owner 待实施项（关联本 change task 3.4）**：模板中的 `{gateway_authority?}` 段当前在 `grammar.py` 的 `parse_vrn` 中**无解析分支**（实测 `boxteam://gw-remote/workspace/ws-1/resources/skills/x/SKILL.md` 的首段被当作 scope 并以 `unknown_scope` 拒绝），故 authority 段仅为契约声明、尚未实现；authority 解析落地前跨 gateway VRN 不可解析。

#### Scenario: resources 固定段不可省略

- **WHEN** 解析一个 VRN
- **THEN** `resources` 段按固定位置参与解析；缺少该段的字符串（如 `boxteam://workspace/ws-1/agent-spec/root/AGENTS.md`）被判定为格式非法并结构化拒绝，而不是回退到更宽松的旧形态

#### Scenario: 既有 skill 形态是特例

- **WHEN** 系统处理一个不带 gateway authority 的 skill VRN
- **THEN** 它由同一条语法规则解析，authority 取本机缺省，而非走独立的历史分支

#### Scenario: 拒绝百分号编码与 fragment

- **WHEN** VRN 字符串包含 `%` 或 `#`
- **THEN** 系统在分段与访问任何资源之前显式拒绝，分别返回百分号编码拒绝与 fragment 拒绝的结构化拒绝码

#### Scenario: 规范化只有单一实现

- **WHEN** 同一逻辑地址以不同大小写或冗余分隔符表达
- **THEN** 所有调用方得到由同一实现产出的同一规范化结果（大小写变体被显式拒绝而非静默归一）；MUST NOT 存在第二处独立的大小写或分隔符处理

#### Scenario: 未登记 kind 被拒绝

- **WHEN** VRN 的 kind 段不在已登记闭集内
- **THEN** 系统返回 kind 未登记的结构化拒绝码，不回退到通用资源读写

### Requirement: kind 闭集定稿且描述符闭集独立不可混用

VRN 的 kind 闭集 MUST 为 `agent-spec` | `skills` | `config` | `session`（依据权威表：既有真实闭集为 `agent-spec`/`skills`，`config` 与 `session` 为本次新增）。`config` 承载配置来源文件本身；`session` 承载会话上下文资源（会话定位，由并行 change `migrate-session-context-uri-to-vrn` 消费其 VRN 表达）。两者均在**本 requirement 登记，无需再由其它 change 新登记**。

系统 MUST 区分**两个独立的 kind 闭集**，MUST NOT 混用：`parse_vrn` 的 kind 闭集（`grammar.py:18` 的 `_RESOURCE_KINDS`）与描述符 kind 闭集（`values.py:28` 的 `_DESCRIPTOR_KINDS`）。**语法闭集 `_RESOURCE_KINDS` 为四值 `agent-spec`/`skills`/`config`/`session`（`grammar.py:22`），描述符闭集 `_DESCRIPTOR_KINDS` 为两值 `agent-spec`/`skills`（`values.py:28`）；描述符闭集原有成员 `memory` 已随提交 32bc6256 物理移除**，语法侧对 `boxteam://memory/{scope}/{name}` 两点式以 `unknown_scope` 类拒绝码 fail-closed 拒绝（已落地），故不再存在「`memory` 只出现在描述符闭集、语法 kind 为 `None`」的错配分支。

#### Scenario: config kind 承载配置来源

- **WHEN** 系统表达一条配置来源资源的地址
- **THEN** 其 kind 使用闭集内的 `config`，与既有 kind 共享同一套语法与拒绝码

#### Scenario: session kind 承载会话上下文资源

- **WHEN** 系统表达一个会话上下文资源（会话定位）的地址
- **THEN** 其 kind 使用闭集内的 `session`，与既有 kind 共享同一套语法与拒绝码；该取值已在本登记处定稿，并行 change 直接引用、无需新登记

#### Scenario: 两个 kind 闭集不可混用

- **WHEN** 实现或测试使用 kind 取值
- **THEN** 语法解析期只接受 `_RESOURCE_KINDS`，描述符构造期只接受 `_DESCRIPTOR_KINDS`，MUST NOT 用其中一个闭集去校验另一个的输入

### Requirement: gateway authority 承载稳定 gateway_id 且缺省等价本机

VRN 的可选 gateway authority 段 MUST 承载**稳定 gateway_id**：段缺省表示本机 gateway；段等于本机 gateway_id 与缺省等价；段等于对端 gateway_id 表示跨 gateway。系统 MUST NOT 为「其它 gateway」引入新 scope，MUST NOT 把 authority 段与 scope 段混为一谈，MUST NOT 让 authority 承载瞬时通道标识（channel instance/epoch/route）。

**实施状态注记（2026-10-01 第八轮）**：当前 `parse_vrn` 无 authority 分支（`grammar.py` 对 `authority` 零命中），故以下三条 Scenario 待 authority 解析（本 change task 3.4）实施后方可判真；本注记只标实施状态，不删除 Scenario。

#### Scenario: authority 缺省等价本机

- **WHEN** VRN 不含 authority 段
- **THEN** 系统按本机 gateway 解析，且与显式写本机 gateway_id 产生相同的解析结果

#### Scenario: 对端 authority 触发跨 gateway 解析

- **WHEN** VRN 的 authority 段等于一个对端 gateway_id
- **THEN** 系统进入跨 gateway 解析链，判定依据仅为该 authority 段，不依据 scope 或 kind

#### Scenario: authority 不承载瞬时通道标识

- **WHEN** 一次跨 gateway 解析经过某条具体通道
- **THEN** 通道实例/epoch/路由 locator MUST NOT 出现在 VRN、持久化记录或业务幂等键中；authority 只表达稳定 gateway_id

### Requirement: VRN 解析必须是星型且以 policy 常量界定上界

VRN 解析 MUST 按唯一顺序执行：本地 parse（fail-closed）→ 无 authority 或 authority 等价本机时由本进程按 workspace registry 解析 → authority 指向对端时按联邦关系转发：本地 gateway 是自身联邦的 **hub** 时可直接解析其**直接 spoke** 的资源；本地 gateway 是 **spoke** 时通过其**唯一 hub** 做**一次有界 transit 解析** → 不可达、未共享或未找到时 MUST fail-closed 返回结构化拒绝码。系统 MUST NOT 回退到本地猜测路径、空路径或任何虚假默认值。

跨边界 transit MUST 携带 `visited set`、`max_transit_gateways`、`max_gateway_hops` 与**总 deadline**，且这些上界 MUST 是**显式策略常量**（集中定义），MUST NOT 硬编码为散落的魔法数字；拓扑变化时 MUST 改策略而非重写解析器。

**解析命中只返回稳定身份与内容，不返回、不携带 locator**：`locator 是输入，不是输出` 是本 capability 可机械检查的不变量。

#### Scenario: 跨 gateway 只传逻辑事实

- **WHEN** 解析请求跨过 gateway 边界
- **THEN** 边界两侧只传递 identity、VRN、revision 与资源内容，MUST NOT 传递 real path、provider locator 或 credential

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

系统 MUST 复用既有拒绝码命名空间与风格，并 MUST 在**唯一一处集中登记处**登记全部拒绝码，且 MUST **分三套独立列出（语法期 / 解析授权期 / 联邦解析期）、标明各自适用范围与「不可混用」**：

- **grammar 拒绝码（17 个）**：`VrnGrammarError.reason_code`，闭集定义于 `grammar.py:25-44`（`empty_uri`、`unknown_scheme`、`scheme_case_error`、`userinfo_rejected`、`query_rejected`、`fragment_rejected`、`backslash_rejected`、`control_char_rejected`、`percent_encoding_rejected`、`non_ascii_rejected`、`empty_segment`、`dot_segment`、`invalid_character`、`case_error`、`unknown_scope`、`unknown_resource_kind`、`malformed_path`）。构造函数对未登记 code 直接 `raise ValueError`（`grammar.py:51-53`），故该闭集**不可扩展**，适用于字符串→`ParsedVrn` 的语法解析期。
- **resolve 拒绝码（6 个）**：`VrnResolveError.reason_code`，闭集定义于 `resolver.py:29-38`（`scope_mismatch`、`unknown_resource`、`unknown_operation`、`capability_denied`、`snapshot_unavailable`、`historical_snapshot_missing`），适用于已解析出 VRN 之后的解析/授权期。
- **联邦解析期拒绝码（第三套，独立闭集）**：`app/gateway/federation/errors.py` 的 `FederationError.code`，**归属 `app/gateway/federation/`**，与上述 grammar 17 + resolve 6 两套不混用。真实码名（实测，含两种命名风格）：`federation-unknown-peer`（未知 / 未登记对端 gateway）、`federation-channel-closed`（channel / 对端连接不可用，即不可达）、`target_not_resolvable`（目标在当前有界拓扑内不可解析，覆盖「未共享 / 未找到」，对「未授权存在」与「不存在」返回同一码）、`target_ambiguous`（裸 session id 命中多个已授权候选）、`federation-deadline-exceeded`（总 deadline 耗尽）、`federation-transit-limit-exceeded`（中继次数 / 跳数超 policy 上界）。注意：`federation-` 前缀**不是本套一致约定**——`target_not_resolvable` 与 `target_ambiguous` 逐字不带前缀。

上述三套闭集 MUST NOT 混用：语法期 MUST NOT 抛 resolve 码或联邦解析期码，解析/授权期 MUST NOT 抛 grammar 码或联邦解析期码，联邦解析期 MUST NOT 抛 grammar 码或 resolve 码。新增码 MUST 只出现在集中登记处一次，其它模块与其它 change MUST 只引用、MUST NOT 自造同义码。

#### Scenario: 三套拒绝码分别登记且不混用

- **WHEN** 需要引用一个拒绝码
- **THEN** 调用方按所处阶段（语法解析 / 解析授权 / 联邦解析）选择对应闭集，MUST NOT 跨集使用，且 MUST 从集中登记处引用

#### Scenario: 未登记拒绝码被视为缺陷

- **WHEN** 某实现抛出一个不在对应闭集内的拒绝码
- **THEN** 构造该错误时即显式失败（现有构造函数已对未登记 code 抛 `ValueError`），不允许闭集被静默扩展

#### Scenario: 其它 change 不得自造拒绝码

- **WHEN** 另一个 change 需要表达一种新的拒绝
- **THEN** 它 MUST 引用集中登记处并说明归属哪一套闭集，MUST NOT 自行发明名称或定义同义码

#### Scenario: 联邦解析期拒绝码引用真实码名

- **WHEN** 一个 scenario 描述跨 gateway 解析失败（未知 / 未登记对端、未共享、不可达、上界或 deadline 超限）
- **THEN** 它 MUST 引用联邦解析期闭集内的真实码名（`federation-unknown-peer`、`target_not_resolvable`、`federation-channel-closed`、`target_ambiguous`、`federation-deadline-exceeded`、`federation-transit-limit-exceeded`），MUST NOT 自造或改写码名

### Requirement: identity 必须独立于 VRN 且不跨 scope 混同

ResourceIdentity MUST 不透明、稳定且 revision-free。同一逻辑名出现在两个不同 scope（例如 `user` 与某个 `workspace`）时 MUST 是两个不同 identity。跨来源的等价与覆盖 MUST 是独立 concern，MUST NOT 塞进 identity 或 VRN 语义。

#### Scenario: 同名跨 scope 是两个 identity

- **WHEN** 同一逻辑名同时存在于 `user` scope 与某个 `workspace` scope
- **THEN** 系统为两者分配不同 ResourceIdentity，且任一方的解析不因另一方存在而改变

#### Scenario: 跨来源覆盖不改变 identity

- **WHEN** 高优先级来源覆盖低优先级来源的同名资源
- **THEN** 覆盖只影响后续 VRN 解析结果与 catalog 快照，既有 identity 与既有已封存绑定保持稳定

### Requirement: 默认寻址政策必须以 VRN 为默认形式

软件内部的配置、skill、状态与资源引用 MUST 默认以 VRN 传递；real path MUST 只在最后访问点出现。任何新增持久化字段若需定位资源，MUST 使用 `identity + VRN(+ 独立 revision 字段)` 组合，MUST NOT 存储 real path。

**迁移面 MUST 表达为「新写字段」而非「存量数据迁移」**：VRN 已确证**零落盘**（157 live 库 + 44 dev/temp 库 0 命中），既有持久化挂点为 `context_source_control_states`（已存来源事实但无 URI 列）与 `resource_activation_bindings.display_uri`（已建表但生产从不写入）。因此实现 MUST 以「加列 + 写路径 + 切换读路径」落地，MUST NOT 写「扫描/规范化/失效既有 VRN 实例」这类空转任务。

#### Scenario: 新增持久化字段不得存 real path

- **WHEN** 一个 change 或实现需要新增一个用于定位资源的持久化字段
- **THEN** 它存 identity 与 VRN（必要时加独立 revision 字段），不存 real path、不存 provider locator

#### Scenario: 模型可见载荷只带 VRN

- **WHEN** 资源引用进入模型可见的 prompt、工具结果或历史投影
- **THEN** 它们携带 VRN 与独立 revision 标识，MUST NOT 携带 real path 或 credential

#### Scenario: 迁移是新写字段

- **WHEN** 某个既有持久化字段需要承载资源引用
- **THEN** 它以 identity + VRN 的新格式新写入并切换读路径，旧写入形态物理下线，且不构造任何存量扫描或数据改写

### Requirement: 配置来源寻址必须使用 config kind 且 sqlite 层不可寻址

配置来源资源的 VRN MUST 标识**来源文件本身**，kind 取自闭集 `config`；`layer` MUST 作为**兄弟字段**保留，MUST NOT 塞进 VRN。

**`inline` 层有 VRN**：它是发行包内真实存在的 JSONC 文件（`configs/workspace_inline.jsonc` / `configs/gateway_inline.jsonc`，经 `resolve_config_resource_source` 的 `is_file()` 校验），有稳定 disk 载体。

**config VRN 的尾段形态（定稿）**：尾段 MUST 取该来源的**逻辑资源名**，MUST NOT 取原始文件名。尾段 MUST 是**单段**、MUST 落在动态段闭合 charset `[A-Za-z0-9_-]` 内、MUST NOT 含点号——真实文件名 `workspace_inline.jsonc` / `gateway_inline.jsonc` 的点号与扩展名不可原样入 VRN（`parse_vrn` 对含 `.` 的尾段以 `invalid_character` fail-closed 拒绝，该 charset MUST NOT 放宽、MUST NOT 新增转义后门）。尾段取值 MUST 与该来源的 `layer` 兄弟字段**一一对应**（`layer` 仍留在 VRN 之外，不进 VRN 字符串）。规范形态即 `boxteam://{scope}/{scope_id}/resources/config/{logical_source_name}`，其中 `scope`/`scope_id` 按本 capability 的 scope 闭集 requirement 取值（`inline` 层取 `scope=inline` 与真实 `distribution_id`），逻辑资源名取该层可寻址载体的逻辑名（如 `workspace_inline` / `gateway_inline`）。

**`sqlite` 层 MUST NOT 被赋予 VRN**：它是**边界变量**而非固定资源——`user`/`user_local`/`workspace` 三层共享同一个 `workspace.sqlite`（`app/services/infrastructure/config_service/` 包（原单文件已拆为同名包）的 `_config_source` 在 state store 存在时统一返回同一个 `path`，同层再按 `layer_names` 映射回三种层名）。把它映射成单一 VRN 会立刻产生「同一 URI 对应四个逻辑来源」的冲突，故 MUST 显式说明其**共享载体导致的不可寻址性**。

#### Scenario: config 资源有 VRN

- **WHEN** 系统为一条 `inline` 层配置来源构造地址
- **THEN** 它使用 `config` kind 的 VRN，`layer` 作为兄弟字段随行，VRN 字符串本身不含 layer 取值

#### Scenario: config VRN 尾段是逻辑资源名而非原始文件名

- **WHEN** 系统为一条 `inline` 层配置来源构造 VRN，其底层真实文件名为 `workspace_inline.jsonc`
- **THEN** 尾段取逻辑资源名（`workspace_inline`，单段、落在 `[A-Za-z0-9_-]` 内、无点号），MUST NOT 取含 `.jsonc` 的原始文件名；含点号的尾段被 grammar 以 `invalid_character` 拒绝，且 charset MUST NOT 被放宽

#### Scenario: sqlite 层不编 VRN

- **WHEN** 系统处理 `user`/`user_local`/`workspace` 这些共享同一 `workspace.sqlite` 的来源
- **THEN** 不为该 sqlite 文件编造 VRN，并显式说明其共享载体导致的不可寻址性

### Requirement: config 的 layer 轴与 VRN 的 scope 轴相互独立且同名不蕴含同义

系统的 config `layer` 轴与 VRN `scope` 轴 MUST 被当作**两个相互独立、不可互换的轴**：`layer` 是 config 来源层身份、由 `app/core/config_sources.py` 的 `ConfigSourceLayer` 与 `app/schemas/internal_v2/config.py:20` 定义；`scope` 是 VRN 寻址身份、由本 capability 的 scope 闭集 requirement「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」定义。两个轴都含 `inline`，且另有同名异义取值——**同名 MUST NOT 被解释为同义、MUST NOT 被当作等价或可互换的枚举**；本 requirement MUST NOT 复述任一轴的取值表（避免制造第二份定义）。

#### Scenario: 同名不同轴不被混用

- **WHEN** 实现或测试同时处理 config `layer` 与 VRN `scope`
- **THEN** 两者各自按自己的轴校验与取值，MUST NOT 用其中一个轴的闭集去校验另一个轴的输入，也 MUST NOT 由 `layer` 取值直接推出 `scope` 取值

#### Scenario: 同一 source_key 在所有读路径报同一逻辑来源层

- **WHEN** 系统分别从源 JSONC 构建与从 active snapshot 基线恢复同一条配置来源，并分别通过 `_config_source`/`_runtime_override_source` 与 `_persisted_source_details` 取得其 `layer`
- **THEN** 两条读路径对同一 `source_key` MUST 报同一个逻辑来源层与同一个 `precedence`；`layer` MUST 是逻辑来源层，MUST NOT 因该来源被同一 `workspace.sqlite` 承载而被改写成 `sqlite`（该共享载体的不可寻址性只以 VRN 缺失表达，MUST NOT 作为有损层名）；`inline` 层的 `precedence` MUST 保持其权威值，MUST NOT 落入任何兜底取值

### Requirement: 既有配置来源持久化必须按同一模式迁移为 VRN 兄弟字段

系统 MUST 消除 real path 持久化违约：配置来源层记录（`app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord`/`ConfigSourceJournalRecord`）MUST NOT 携带 `source_path`/`backup_path` 一类真实路径字段；配置来源列表 API 响应体（`GET /api/v1/config/sources`，经 `app/api/config.py` 的 `ConfigSourceDTO.path` 与 `ConfigSourcesDTO.schema_path`）MUST NOT 输出真实路径。**该违约已由 `50bffa45`（持久化侧：`source_path`/`backup_path` 物理删除、改 `vrn: str | None`）与 `76ed0089`（响应体侧：`path` 与 `schema_path` 均改配置来源 VRN）消除**；本节保留为可机械复核的不变量，实施期若回退即缺陷。

迁移 MUST **直接复用 config 侧既有的平级属性模式**（`app/core/config_sources.py:16` 的 `ConfigSource` 已是 `path` + `layer` + `precedence` 平级，且 `layer_revision`/`layer_digest`/`source_generation` 已是兄弟字段），即「把 `path` 换成 `vrn`，其余 sibling 字段原样保留」。MUST NOT 另发明第二套表示。

#### Scenario: 配置来源不再持久化 real path

- **WHEN** 一条配置来源被写入持久化记录
- **THEN** 记录中承载 VRN 而非 real path；real path 只在读取该来源内容时于调用栈内出现

#### Scenario: API 不输出配置来源真实路径

- **WHEN** 客户端请求配置来源列表（`GET /api/v1/config/sources`）
- **THEN** 响应体只含 VRN 与兄弟字段（layer/precedence/layer_revision/layer_digest/source_generation 等），不含真实路径；响应体的 **每一个** 承载来源位置的字段（含 `ConfigSourceDTO.path` 与 `ConfigSourcesDTO.schema_path`）都 MUST 是 VRN，MUST NOT 存在承载 real path 的字段

#### Scenario: 复用既有平级属性模式

- **WHEN** 实现配置来源的 VRN 化
- **THEN** 它沿用既有 `path`→`vrn` 的平级属性替换，其余 sibling 字段（layer、precedence、revision、digest、generation）原样保留，不新增第二套结构

### Requirement: 多工作区场景下寻址层必须显式承载 scope_id 身份

**归属与引用**：本 requirement 的多工作区前置条件由 `add-multi-workspace-backend-mounting` 承载；其 requirement `进程内必须维护权威的已挂载工作区注册表`、`workspace 身份必须由显式寻址载体承载`、`workspace_id 必须与 VRN workspace scope 使用同一份身份`、`持久化数据不得以「当前激活工作区」为前提` 是本 change 多工作区部分的引用来源。本 change 只声明寻址层要求，不复制其实现要求。本 capability 的 scope_id 取值表 MUST 与 `add-multi-workspace-backend-mounting` 的挂载模型保持一致，MUST NOT 各自定义 scope_id 取值语义。

在一个后端进程可挂载多个工作区的前提下，scope_id 身份 MUST 在寻址层显式表达（HTTP API 与 VRN 皆然）。系统 MUST NOT 依赖「当前激活工作区」作为持久化数据的前提。

#### Scenario: 单进程多工作区各自显式寻址

- **WHEN** 同一后端进程同时挂载多个工作区
- **THEN** 每条对工作区资源的 VRN 都在 `scope_id` 段显式携带对应 workspace_id，解析结果不随「当前激活工作区」切换而改变

#### Scenario: 持久化数据不绑定激活态

- **WHEN** 一条持久化记录引用工作区资源
- **THEN** 其含义只由记录内显式的 scope/scope_id 与 VRN 决定，与记录写入时或读取时的「当前激活工作区」无关

### Requirement: 正名 builtin 到 inline 与 layer 改名必须同步且不得误改无关同名

系统 MUST 把 VRN scope 由 `builtin` 正名为 `inline`，并 MUST 把 skill catalog 的 `layer` 名同步由 `bundled` 正名为 `inline`（理由：同一概念三个名字——layer `bundled`、scope `builtin`、config layer `inline`——正是本仓库要求根除的）。两处都改名后，`app/agents/skill_runtime.py` 的映射 shim 退化为恒等映射并**已被 `298ef599` 物理删除**（`layer_order` 改为 `(inline, gateway, workspace)`，`scope = {...}[layer]` 改名 shim 已不存在）。

改名 MUST 带影响评估结论并据此定级：`layer` 进入 `entry_identity`（`skill_runtime.py:558`）与 catalog payload（`:605`），但二者只进**内存** `ResourceRegistry`（`semantic_registry.py:20-23` 三个 dict 无持久化写入），全仓唯一持久化 `display_uri` 列的写入方生产从不被调用，故本改名属**契约级调整而非数据迁移**，MUST NOT 构造存量迁移任务。

**真依赖 VRN scope `builtin` 的位置**：`grammar.py` 现**已无 `builtin` 字面量**（`rg 'builtin' grammar.py` 零命中），scope 闭集 `_SCOPE_KEYWORDS`（`grammar.py:18`）已是 `{workspace, user, gateway, inline}`；改名的实际约束落在 `resolver.py` 的 scope 构造校验与 `require_scope_binding`（闭集映射已含 `inline`）。其余 `builtin` 命中（工具 `origin="builtin"`、主题来源、`builtin_tool_registry` 等）是无关同名，MUST NOT 误改。

#### Scenario: scope 与 layer 同步正名

- **WHEN** 系统产出 Skill 来源标识
- **THEN** scope 使用 `inline`、layer 使用 `inline`，MUST NOT 继续产出 `builtin` scope 或 `bundled` layer，且不保留运行时别名

#### Scenario: 改名不构造存量迁移

- **WHEN** 实施 scope/layer 正名
- **THEN** 因相关字符串只进内存 registry 与响应、不落盘，MUST NOT 扫描或改写任何既有持久化记录

#### Scenario: 无关同名不被误改

- **WHEN** 实施改名时检索 `builtin`
- **THEN** 只改真实依赖 VRN scope 的位置（`grammar.py:18` 的 scope 闭集 `_SCOPE_KEYWORDS` 与 `resolver.py` 的 scope 构造校验；`grammar.py` 已无 `builtin` 字面量、`_SKILL_SCOPES` 已不存在），工具 origin、主题来源与 `builtin_tool_registry` 等无关同名保持不变
