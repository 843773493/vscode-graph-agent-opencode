## Context

用户已确认插件体系自有化，具体动机见 proposal.md。当前主链包括 `app/gateway/runtime` 的进程／日志管理、`app/core/child_process` 的跨平台原语、独立 Terminal／Browser 服务、Backend 中的 MCP owner 和 stdio transport，以及尚未完成的 VSIX／Extension Host 接入。以上是待迁移的事实，不是两套长期支持的模式。

本 change 保留原目录及三个新增 capability 标识以维持引用，重新定义其规范内容。tasks.md 的旧验收记录是历史证据；当前任务均按新边界验收，不把原 Node Extension Host、VSIX 或第三方目录的通过记录作为新合同通过。

## Goals / Non-Goals

**Goals:** 借鉴完整扩展架构，区分声明式贡献、运行期 Provider 与受管服务，首批开放少量能力；直接复用已有 Code OSS 注册表及服务；统一服务进程与访问边界，Python／Node 使用同一合同；插件仅采用项目格式，提供自有公网来源占位；保留工作区业务 owner 和资源，以小规模真实入口测试证明正确性和总延迟。

**Non-Goals:** VSIX 或 vscode API 兼容、第三方插件市场、通用脚本远程执行平台、自动修复／高可用、通用跨进程操作续跑、任意下载代码向 renderer 注入、Electron 原生插件宿主；不改动 python_exec、持续任务条件脚本和视频 ffmpeg 的调用方式、Backend 执行位置或进程管理。高级恢复或服务专属生命周期只在后续有实际需求时扩展。

## Decisions

### D0. 统一概念与管理单位

插件包 Plugin Package 是一个 ZIP 的安装、整体更新和卸载单位；插件 Plugin 是包内独立启用、停用和重启单位；贡献 Contribution 是插件提供的 Skill、工具关联、命令、菜单、配置或服务；运行实例 Runtime Instance 是插件在明确 Gateway／workspace 绑定中的一次运行。UI 用插件包分组，组内列各插件开关，实例只展示状态、日志和错误。manifest 根以 package_id 标识包，plugins 数组声明包内插件；外部绑定使用 package_id／plugin_id；Module 只用于源码组织，Feature 不作为此合同实体，Capability 保留给协议能力和权限。纯声明插件不创建运行实例。

```text
插件包 Plugin Package：开发工具集（安装／更新／卸载）
├─ 插件 Plugin：浏览器（Skill、工具关联、配置、服务）
│  └─ Runtime Instance：按声明范围和明确目标运行
├─ 插件 Plugin：终端
│  ├─ Runtime Instance：工作区 A
│  └─ Runtime Instance：工作区 B
└─ 插件 Plugin：源码调试
```

包组标题的“全部启用／停用”逐项管理当前明确范围中的插件，不增加包级运行开关；用户直接操作“关闭浏览器插件”。


### D1. 管理入口与业务流量都经过目标 Gateway

```text
Backend ── HTTP／SSE／WebSocket ── Gateway ── 精确服务实例
                                    ├─ Gateway 范围服务
                                    └─ 工作区范围服务
插件实例 ── 持有 PTY／浏览器／stdio 管道 ── 实际子进程
A Gateway ── 已认证联邦连接 ── B 原生 Gateway ── B 的服务实例
```

Backend 消费稳定服务登记身份和运行身份，不持久化地址或凭客户端 URL 访问插件。Gateway 从唯一登记选择原实例并转发，后续流式连接保持原目标；不存在或离线明确报错，不绕过 Gateway、不回退本机、不在普通读取中补启动。外部 HTTP MCP 同样由 Gateway 代理，远端地址属于服务连接定义，不创建无必要的本地包装进程。

Gateway 负责传输、实例路由和生命周期，不解析工具策略或写会话历史。Terminal／Browser 的业务 owner、资源关联及持久状态仍沿原服务和工作区合同，Frontend 的交互连接也走 Gateway。用户显式调用的终端命令在终端服务中执行，不能因 Backend 不创建进程而转成 Gateway 的任意 shell API。

### D2. 包、插件、服务登记与运行实例

| 对象 | 稳定归属 | 生命周期 |
|---|---|---|
| 插件包 Plugin Package | 安装域、package_id、确切版本／摘要 | 整包安装、更新、卸载；插件不独立下载或版本化 |
| 插件 Plugin | package_id + plugin_id；生命周期槽位另含原生 Gateway 和必要 workspace_id，包版本是该槽位的绑定属性 | 固定 instance_scope；独立启用、关闭、重启 |
| 贡献 Contribution | 原插件下的稳定贡献 ID | 随插件启用发布，停用撤销新使用准入；配置 schema／诊断声明可保留只读展示 |
| 服务登记 | 上述插件绑定及 service_id | 为具名协议路由，不另设独立服务开关或启动策略 |
| 运行实例 Runtime Instance | 插件绑定 + 每次启动唯一 instance_id | 一个有运行代码的插件绑定只有一个 runtime；重启换身份；进程可持有 PTY／browser／adapter 等子进程 |
| 业务资源 | 原工作区／Session／Thread／服务资源 ID | 仍由原业务 owner 管理，不按 UI 焦点改投 |

每个 package_id／plugin_id／原生 Gateway／必要 workspace_id 组合只有一个生命周期槽位；确切包版本是槽位当前内容及注册／调用核验属性，不另开按版本并行的槽位。版本切换仍须结清同槽位旧实例。

同包插件各有独立入口和进程，不能共用一个 runtime 再在内部实现部分停止；相关工具和 Skill 可放进同一插件，不按每个工具拆进程。插件固定声明 gateway 或 workspace 范围，用户不能通过开关改变范围。gateway 插件在原生 Gateway 的绑定下唯一运行；workspace 插件按明确工作区独立运行。相同机器不同 Gateway／Home 不合并。共享包或 Gateway 进程不等于共享工作区资源／凭据。

启用状态只由原配置 owner 持有，UI 开关经配置提交后交 Gateway 执行，不另建启用账本。Gateway 插件随所属 Gateway 生命周期恢复；workspace 插件随工作区运行时启用／完整停止恢复和停止。单独 Backend 重启、页面关闭、Tab／导航切换不改变插件启用状态或停止实例。工作区完整运行时恢复时，由 Workspace 配置 owner 重新交付已提交的非秘密插件绑定，Gateway 校验后才恢复启动；owner 未就绪／不可达则显示等待配置交接，不读 .boxteam、不从旧进程或安装库存推断 enabled。Backend 单独重启和 Gateway 正常换代接管时，已核实原实例按原接管合同保留，不因等待交接杀掉、改绑或另起实例。

只有插件开启／恢复及显式重启可启动代码；命令、工具、Skill、普通读取一律不能触发启动。

### D3. 自有 plugin.jsonc 与插件入口

包使用标准 .zip，根 plugin.jsonc，不规定专用后缀。开发目录使用同一结构和合同。根字段只有包级 manifest_version、package_id、version、名称／说明、extension_contract_version 与 plugins 数组；不保留根级 runtime／entrypoint／instance_scope／contributes，也不双读此前未发布的单入口或 components／component_id 设计。

每个 plugins 项声明稳定 id、名称、固定 instance_scope、requires 和 contributes；其 id 就是外部绑定的 plugin_id，不另设第二标识。插件 ID 包内唯一，由 package_id／plugin_id 组合确定逻辑插件；原扩展点版本及七类贡献均放在 plugins[].contributes 内。配置 schema 只在该插件的 contributes.configuration 中，用户参数／开关不写入安装包。命令处理者或服务需要代码时，该插件另声明 runtime、entrypoint、management_protocol_version；runtime 支持 python／node，entrypoint 是相对包根的入口及参数数组，不是 shell 字符串。平台／运行环境要求归需要代码的插件；纯 Skill／菜单／配置插件没有这些运行字段。插件不得引用兄弟插件的私有 handler 或共用其进程；v1 不实现插件依赖自动启用。

安装按包校验 manifest、资源路径、摘要和所需合同，准备锁定依赖后原子发布唯一安装记录。未知结构、VSIX、非法插件或不兼容合同明确拒绝，不部分安装、不自动转换。目标运行环境和发行资源提供解释器／依赖，首次工具调用不下载；安装准备子进程仍在受管边界，不执行任意安装 hook。普通请求不重新 hash 全包。自有贡献直连原 Workbench／业务服务，不提供 vscode API、Extension Host RPC、VSIX 转换或任意 renderer 脚本注入。

通用插件秘密参数采用 D9 的秘密输入、存储与运行交付合同，支持用户直接输入后安全保存，也保留 `${ENV_NAME}`／`env:NAME` 的既有环境引用。manifest 的 writeOnly 在本项目表示秘密输入及禁止普通读取回显，不是禁止用户经受保护入口输入真实值；普通 JSONC／参数通道仅接受合法引用。schema 声明不是秘密存储实现，不按字段名猜测秘密，也不在普通文本中自动展开环境变量。

### D4. 控制合同与业务合同各有版本

所有运行代码的插件提供同一管理合同：启动就绪回执、status、shutdown、诊断配置 apply／status，以及实际支持的统计读取。管理回执以插件实例为身份，包含 package_id／版本、plugin_id、作用域、instance_id、管理及扩展合同版本、状态、能力及可为空的已导出服务清单；命令处理代码插件可以不提供任何服务。service_id 只在服务清单和具体业务路由内使用，不作为插件管理操作的单值身份；未知或不兼容版本在就绪前拒绝。纯声明插件没有 ready 进程，其贡献状态独立于运行状态。逻辑管理操作是具名协议，不开放任意模块／method／命令透传；插件命令只经 D13 的已注册命令合同调用。Python／Node 复用同一 schema 与合同测试，各自只实现必要语言侧支持。

业务协议按服务类型声明：MCP 使用标准 Streamable HTTP；Terminal 保留 HTTP 控制与交互 WebSocket；Browser 保留 HTTP 操作及画面／输入通道；Debug 使用 D16 的 DAP 和具名调试操作。每个连接路由到原实例，保留背压、取消、请求 ID、资源 ID、输出序列和错误归属。流连接的握手鉴权后仍保留原连接租约／撤权边界，不为每帧创建控制请求或扫描进程。

MCP 插件内的 stdio 桥接必须保真处理初始化／能力协商、分页目录、变化通知、进度、取消、媒体、资源，以及服务声明支持的 server-to-client 请求。只包装 list/call 不代表完整支持；能力不支持要明确拒绝／不宣称支持。真实业务调用沿 Backend 唯一 MCP client 和目录 owner，Gateway 不建立第二工具目录。

### D5. 只按启用状态管理生命周期

用户只管理插件开启／关闭和显式重启；不提供 automatic／on_demand／manual 多策略、不按首次工具或命令访问激活、不因 Skill 加载启动代码。进程实际状态仍是 stopped／starting／ready／stopping／failed，连接可用性独立报告；配置期望 enabled 与实际状态分开，启动失败保持 enabled 并显示 failed，不伪报成功或自动改回关闭。纯声明插件只显示已启用／已停用，无进程 ready。

| 事件 | 默认结果 |
|---|---|
| 开启插件 | 原配置提交 enabled=true；所属范围运行时可用则由 Gateway 启动一次；范围尚未运行则显示已启用／等待所属运行时 |
| 关闭插件 | 提交 enabled=false，停止新调用和新 Skill 使用准入；在一个总期限内取消在途工作、关闭自有资源并核实退出，失败显示 stopping／failed 和真实存活状态 |
| 重启插件 | 仅已启用有代码插件可重启，先核原实例退出再启动新 instance_id，enabled 保持；只影响该绑定且保持当前绑定包版本 |
| Gateway／完整工作区运行时恢复 | 读取已提交 enabled 恢复相应插件；正常接管复用核实后的原实例，不重复创建 |
| Backend 单独重启／页面关闭／Tab 切换 | 不停止插件或新建资源；恢复原连接或读取实际状态 |
| HTTP／流断开或单个请求取消 | 不推断进程退出；失败／未知结果不自动重发；取消只影响原操作 |
| 插件崩溃 | failed 并显示原退出信息，不循环自动拉起；用户可显式重启 |

同插件绑定的开关／重启／配置替换由原 owner 串行处理，核原配置 revision 和当前期望，不新增状态机／持久参与者账本。启动中关闭、停止中再开启不能产生两个实例；旧 ready／事件不得复活已停用绑定。并发配置冲突按原 CAS 拒绝，不放弃原操作身份自动重试。关闭只取消该插件自有工作，不杀兄弟插件或未授权 attach 目标；历史和用户数据保留。重启不恢复旧调用，不保证原 PTY／浏览器／MCP 会话存活，实际可查询／attach 能力沿原合同。

### D6. 遥测、统计与日志沿 Gateway 配置热应用

复用现有 `logging.runtime` 的配置 owner、revision 和真实进程热应用确认。新增字段按配置技能放在功能域／子系统下；本方案使用 `telemetry.runtime` 管本地采集，`statistics.runtime` 管请求统计，`plugins.sources` 管自有源，不新增第三套配置文件或覆盖旧日志语义。`telemetry.runtime.enabled` 默认 false，`sample_ratio` 默认1且范围0–1，`sample_buffer_limit` 默认256且范围1–4096；`statistics.runtime.enabled` 默认 false，`collection_interval_ms` 默认5000且范围1000–60000。关闭时不建立这些采集缓冲和定时任务。它们允许 Gateway 原 user／user_local 合法覆盖，不接受 Workspace 层覆盖；对象合并、数组替换沿原配置合同。

Gateway 汇总代理的请求次数／总耗时／错误和现有进程资源数据；插件按能力提供内部阶段指标，不能将代理耗时宣称为插件内部耗时。秘密、会话正文、工具结果及完整终端输出不属于通用遥测。第一版只本地诊断，不把自有公网插件源当遥测上报地址。

关闭时停止对应采样／定时任务和额外统计工作，保留必要错误报告；插件新启动取得当前配置，存活实例按 instance_id 返回实际应用 revision。部分实例失败则逐项显示，不伪造全局应用成功，不杀服务逼迫更新，也不新增分布式事务。遥测不要求普通读写前先做额外完整性检查。

### D7. 自有来源占位与安装生命周期

当前公网源不存在。`plugins.sources` 的正式来源记录包含 source_id、协议版本、可选 base_url 及 enabled；内置无虚构地址，未配置／未启用时市场 UI 显示明确状态并且零网络请求，本地包、开发目录及已安装管理照常可用。预留的目录／下载合同提供来源、分页／目录版本、package_id／版本、平台／运行时要求、包 URL、`.zip` 文件名与摘要，文件名不参与插件身份判定；查询变更取消或废弃旧结果，不因零命中查其它源。公网服务建设另立任务，不在本轮创建在线目录或默认凭据。

安装／更新在发起时冻结目标安装域、来源、确切版本和摘要。下载、校验、依赖准备及原子发布由唯一安装 owner 负责；安装完成不自动启用工作区或运行插件。Bun／uv 仅用于自有包的锁定依赖准备，插件运行仍使用目标 Node／Python，发行包明确提供安装工具位置并复用正常缓存；安装配置和凭据独立隔离，不执行任意安装 hook，仅允许具名 node-pty 的受控原生构建，不保留独立第三方 MCP 目录安装入口。

更新仅安装新版本，不修改绑定、模板或现有实例。切换绑定版本是独立显式配置操作，在一个绑定内按下表执行；不同插件不要求全局串行。原配置 owner 只持有一个期望版本，实际进程与各贡献 owner 的发布结果直接报告，不增加可变的版本组合账本。

| 步骤 | 唯一责任与失败出口 |
|---|---|
| 校验候选 | 原配置及贡献 owner 校验安装版本、范围、完整声明和可离线验证的参数；此时不撤旧注册、不停进程。失败直接拒绝，原绑定及发布保持 |
| 提交与撤准入 | 原配置 writer 经 CAS 提交期望版本，撤销该绑定的新业务准入；已关闭插件到此只改配置，不启动或发布可执行贡献 |
| 替换运行实例 | 已开启代码插件交原 Gateway runtime 停旧、核退出、启新；停止失败阻止新实例，启动失败报告 failed。已提交期望不回滚，不重启旧版本；纯声明插件跳过此步 |
| 发布贡献 | 各原 owner 使用 D13 的候选发布步骤及固定引用顺序；MCP 目录另由 D8 的原 Backend 配置流程整体发布。某一步失败即结束本次后续推进，保留已经发生的真实结果，不跨 owner 回滚或补偿 |
| 开放使用 | 只开放目标、期望版本、实际发布及必要运行状态均匹配的调用；失败部分及旧版本残留仅作真实状态／诊断，不能借新实例执行旧声明 |

用户显式重启代码插件可按当前绑定重新应用；纯声明插件关闭后再开启重新发布。切回旧版本是另一次显式配置操作，不能由异常分支代用户发起。普通重启不升版，模板版本另行编辑；不增加通用 retry／rollback／reconcile 引擎。各 owner 的部分结果不构成跨进程事务，不改写已冻结历史。

卸载只检查四类真实阻塞引用：运行中或尚未核实退出的实例；配置 owner 的 active／pending 绑定（关闭但未解除的绑定也算）；模板及未结清创建 intent；原资源 owner 的未完成读取／使用 lease。仅为展示的可释放投影先在卸载准入处结清，完整封存正文和未绑定的参数残留不占安装引用。唯一安装 owner 沿既有登记及交付关系定位相关 owner，在原拓扑／版本删除互斥边界内核引用并删除；引用未知或 owner 无法确认时拒绝本次卸载并指出来源，不假设为空、不启动后端查引用。普通列表／调用不对账，不新增全域扫盘、后台巡检或引用账本。v1 不执行任意安装／卸载 hook；删除包不删除配置、秘密或业务数据，清除数据须另有明确操作。

### D8. MCP owner 与通用托管相接

Backend 原 MCP owner 保留每工作区唯一连接、完整目录发布、目录 revision、connection generation、权限／OAuth、封存调用资格和结果结算。连接经 Gateway 指向精确 instance_id；Backend 重连生成新的 client connection generation，即使插件子进程仍存活。宿主、stdio 子进程或 MCP 会话发生重建时必须失效旧执行句柄，不按同名／同 schema 改投。

stdio 创建及关闭从 Backend 移入所属插件。真实秘密由原工作区 private-input owner 解引用，经已认证 Gateway 在启动准入时只交目标服务，Gateway 不持久化工作区秘密；Gateway 启动所需的非秘密配置与可解引用输入分阶段准备。控制通道可用与业务 ready 分开，输入未到位时明确 starting／等待配置，不把业务报 ready 或要求 Gateway 读工作区文件；待输入服务不得成为 Backend 初始化或其它服务启动的前置门，避免互相等待。Backend 停止后已准备的服务不因此停止，重启新实例所需的秘密由原 owner 再交付。

MCP 参数替换复用原候选与 active promotion；它不修改插件 enabled，也不凭旧 apply claim 获得启动资格。准备、停止、启动及发布边界均核同一插件绑定当前 revision／enabled；用户关闭后旧候选不得重新启动或发布目录。关闭不等待候选完整目录或授权就绪；已关闭插件的参数只做可离线校验和配置提交，不初始化 client／目录，待后续显式开启再验证运行态。需要进程替换的候选沿 D5 先停旧再启新，影响同插件多个服务时明确显示插件级中断，不停止兄弟插件。

参数替换只有一条协调链：Backend 原配置 owner 的 applying journal／claim 驱动原 MCP 候选准备，必要时调用 Gateway 单插件生命周期，随后由原 MCP owner 验证完整目录，最后在原 active promotion／publication 边界统一发布。Gateway 只返回停止／启动事实，不持有候选、active 或目录提交阶段。破坏动作前失败保留可保持的旧运行；动作后失败由原 journal 标明不可用／需人工处理并结束，不自动拉回旧进程、重放调用或后台续完候选。Backend 重启只沿原 journal 判断实际提交阶段和重建连接，不新增跨进程提交、补偿或恢复状态机；插件关闭使旧候选失效并优先撤准入，不能排在候选就绪等待后。

外部 HTTP MCP 配置经同一 Gateway 服务路由使用，OAuth 继续由原工作区 owner 管理；Gateway 不接管第三方认证语义。Backend 的会话／工具结果仍归原 canonical writer，服务存活不恢复旧 execution，结果未知不重放。保留既有资源 lease，不增加通用持久参与者账本。

### D9. 原管理 UI 中的插件开关与设置

Extensions 视图复用原搜索、包列表／详情和目标 Quick Input，直接消费自有模型。包行提供安装／更新／卸载，详情按插件展示固定范围、启用开关及可用的重启动作，并按精确运行实例查看状态、日志和错误；开启不等于 ready。Gateway 和 workspace 插件使用相应管理范围，不能在当前工作区关闭共享 Gateway 实例。包级“全部开启／关闭”只是逐插件批量操作，只选择当前管理目标中固定 scope 匹配的插件；当前 Gateway 排除 workspace 插件，当前工作区和新建默认值排除 gateway 插件，模板操作只修改模板条目。执行前列出实际目标、纳入及排除项，执行后逐项报告，不能跨范围隐式追加；范围内权限不足／失败仍逐项报告，不能伪装成范围排除。不新增包级 enabled、全有或全无事务或隐式自动启用依赖。

UI 管理范围只有“当前 Gateway”“当前工作区”和“新建工作区默认值”。前两者改变精确绑定；第三者只编辑创建模板，不是第三种运行 scope，不修改已有工作区、不按每次启动动态继承。细节由 D19 定义。初次目标为当前入口原生 Gateway；各视图独立保存目标，已有详情、草稿、在途开关冻结原目标，切焦点不能改投，迟到结果不串列表。工具或命令不能代用户开启插件，关闭时明确返回插件未开启／正在停止／失败。

插件参数和启用状态归原 Gateway／Workspace 配置 owner：Gateway 范围在 Gateway 来源，workspace 范围在精确工作区来源，秘密只存原私有引用。UI 开关与 JSONC 共用 writer、CAS、候选及生效合同，不在前端伪造成功。静态配置 schema／诊断信息可在插件关闭后查看，不意味着启用其工具、Skill 或快捷键；未知字段按原 schema 拒绝，未绑定参数仅按下述登记容器保留。Workbench 主题／布局／快捷键仍归原 profile，旧 VSIX 配置桥迁齐后删除。

秘密输入沿原设置表单／Quick Input 使用密码控件。已保存字段只显示配置状态和引用，提供更换、清除入口，不读取原值填回表单；JSONC 仍编辑引用。所有输入交互确认后，专用受保护写入口才接收真实值，核原 configuration_write 和冻结目标、来源、确切包版本 schema，再交所属配置 owner 保存并只返回引用。用户取消输入不写秘密或来源，临时输入不进入浏览器持久草稿、普通事件、诊断或日志。秘密引用仍经原 source writer、预检、CAS、active／pending 提交；保存秘密文件不等于配置生效或插件启动，CAS 冲突保留旧来源，不覆盖并发提交。

秘密正文复用现有权限受限私有文件与原子发布原语，Gateway 范围归目标 Gateway，workspace 范围归对应 Workspace；不以浏览器存储、包目录或接管材料作为秘密来源。增加正式插件参数引用类型，绑定原生目标、必要 workspace、package_id／plugin_id、参数位置及不可变值版本，不复用只绑定 MCP Server 的引用，也不接受任意文件路径。更换秘密创建新版本，再通过原配置 CAS 切换引用；清除只移除选定来源层的引用并展示真实下层值，不自动擦除其它来源或仍被 active／pending 引用的正文，未引用值沿原私有输入 owner 清理规则处理。安装包卸载不顺带删除秘密。首版的保护来自原权限／ACL，不宣称已接入 VS Code 桌面版的系统加密存储；不新增秘密账本、通用 SecretStorage SDK、密钥链框架或跨机器同步机制。

配置 owner 只在声明的秘密字段解析已保存引用或显式环境引用，按目标机器环境解释 `${ENV_NAME}`／`env:NAME`，不读取浏览器或 Relay 的环境。沿用本项目现有语法，不为了对齐 VS Code 的 `${env:NAME}` 再加一组别名。解引用后的实际值按当前确切包版本 schema 验证，错误不携带原值。普通来源、active／pending、事件和接管材料只存引用及原有非秘密元数据；配置读取、安装或运行管理资格均不授予秘密回读。

工作区秘密由原 owner 在已认证启动／配置应用交付中仅传给对应目标插件，Gateway 可沿认证通道转送，但不读取工作区秘密文件或持久化原值。正常接管保留已核实实例；显式重建由原 owner 重新解析已保存输入，合法引用无需再次询问用户。缺输入显示等待输入并提供显式配置入口，缺失版本、权限或归属错误保留明确原因，不改投其它目标、不静默改用环境变量，也不在状态读取、后台恢复或重试循环中反复弹密码框。更换／清除后的实例变更仍沿原配置应用与重启分类，不保证正在运行的插件立即擦除已经取得的值，不自动重放原业务调用。

插件参数必须位于原配置 owner 正式登记的 package_id／plugin_id 参数容器中，与引用安装版本的运行绑定分开。卸载须先解除绑定；剩余参数仍在原来源内，UI 明示“插件未安装／未绑定，参数未生效”，不参与 effective／active／pending 或热应用，不提供已删除 schema 的表单。原 JSONC writer 仍按容器结构及 JSON 类型编辑／删除这些不生效的值；将来显式绑定时再按确切版本 schema 完整验证，失败不启用。这个有限容器不是未知配置兜底，域外字段及已绑定插件的非法参数仍拒绝；不保存影子 schema、额外配置文件或孤儿参数账本。

快速访问沿原 Registry／Command／Opener 提供插件包／插件管理入口，命令调用仅访问已开启且处理者已就绪的绑定，无隐式激活。来源读取、pending、fan-out、权限和 request_id 保持原合同；普通工具调用仅核必要业务准入，不要求调用者拥有启停管理权限。

### D10. 单链路替换、删除清单与发行

| 纵向链 | 迁入方向 | 必须物理删除的旧接入 |
|---|---|---|
| 格式／来源／安装 | 自有 manifest、归档和唯一安装 owner | VSIX／Gallery／Open VSX／第三方 MCP 目录、scanner／profile 安装、vscode:uninstall |
| 服务运行 | Gateway 托管及项目管理协议 | 用户 Web／Node Extension Host、RPC／API 适配、running location／activation／动态 Provider 桥 |
| Backend 消费 | Gateway 的具名 HTTP／流路由 | MCP stdio 创建、Terminal／Browser 直连、Node 调试目标创建、Backend 安装 subprocess 旁路；不包含 python_exec／条件脚本／ffmpeg |
| UI／设置 | 原 UI 组件＋自有模型与原配置 owner | 假 ILocalExtension／VSIX 对象、旧安装库存／配置镜像和宿主 profile／view 文件接口 |
| 协议／发行／测试 | 同一格式与跨语言合同 | 旧 DTO／proto／API、转换／打包入口、锁依赖及只验证旧体系的测试 |

全仓检索直接与间接调用方、生成物、脚本和测试后逐链迁齐，最后删除旧实现，不保留开发兼容层。保留原 Workbench Registry／服务、产品内置贡献及已接入的业务实现；被删除的是旧用户插件格式、宿主和 API／RPC 接线，自有贡献按 D12 接到原机制，不重做现有能力。Code OSS 内部必需依赖可作为固定上游构建输入保留；项目不再注册／启动旧用户插件服务或打包无人消费的宿主产物。reference_repo 与固定上游源码保持只读。

旧数据按最近两个经用户验收 release 的真实契约评估，缺少基线则报告并在隔离 Home 实施，不推测开发残留需要迁移，不删除用户配置／凭据／会话。确需迁移的支持数据用显式官方迁移收敛；不将旧 VSIX 自动转换或继续运行。tasks.md 保留旧证据原文，其通过范围限于原架构。

Web／Electron 共用同一 renderer，插件运行在目标 Gateway 所在环境。发行物包含工作台静态资源、插件启动所需组件及自带插件，离线自有包不依赖源码／开发服务。Python／Node 语言实现不改变协议和身份规则。

### D11. 验证与总延迟

采用少量有判别力的真实产品切片：Gateway 与 workspace 两种实例；同名跨工作区隔离；终端输出／重附加；浏览器操作／重附加；标准 MCP＋stdio 子进程；源码调试 UI 与 Agent 共用同一 run；断连、原实例重连、显式重启、新代际与未知写入不重放；遥测开关与部分失败；原 UI 的自有包安装／配置；旧入口不可达。各链路的实际退出与边界按 D16／D17 验，不把 Backend 所有内部进程一概视为插件执行；python_exec／条件脚本／ffmpeg 不新增插件迁移验收。

通过实际 Gateway、目标工作区、完整 Workbench 初始化验证，不以健康接口或空壳启动代替功能。Python／Node 共用合同场景，Integration 和真实外部 E2E 分别表述。Web／Electron、Linux／Windows／SSH 按实际消费能力分别验收，不以浏览器 parity 代替原生产物。

普通暖读遵守 enforce-interactive-latency-budgets 的现有总预算：普通本地 API p95≤500ms／单次≤1500ms，导航／实际可操作 UI 沿原更严格期限；排队、Gateway 转发、校验、序列化及内部重试都计入。安装和显式启动单独计时，不能把等待塞到普通请求。默认不每请求探活、扫进程或全包 hash；只在安装发布、启动绑定变化和实际恢复边界作必要检查。测试用进程外超时和内存保护，失败先归因，没有输入变化不重复长跑。

### D12. 完整扩展模型与首批开放集合

采用三层模型：声明式贡献由产品解释并注册到原 Workbench 服务；运行期 Provider 经项目具名合同提供数据、操作和事件；实际进程由 Gateway 托管。它们共用包身份、扩展点目录和释放规则，不建立独立插件 UI 框架。Code OSS 的 `ExtensionsRegistry`／`ExtensionPoint` 提供声明收集、校验和增删通知的参照；自有 manifest 由项目贡献 owner 解释，直接调用原 Commands／Menu／Keybinding／Configuration 等服务，不伪造 `IExtensionDescription` 或运行旧 Extension Host。

首批 `plugins[].contributes` 只接受 `commands`、`menus`、`keybindings`、`configuration`、`services`、`debuggers`、`skills` 七类。命令声明稳定 ID、标题／分类、参数和结果 schema、所需业务能力及处理者；处理者在所属插件的 Gateway runtime 内具名注册，不能指向任意模块、shell 或未导出的产品内部命令。菜单声明位置、分组、命令与 when，快捷键声明平台键位与 when；只允许产品公布的菜单位置、上下文键和命令引用，复用原表达式解析与用户快捷键覆盖，不执行表达式代码。配置继续提交原域 owner；服务声明类型、协议及范围，命令处理可复用同一 runtime，不新建命令专属 supervisor。`debuggers` 声明自有调试类型、配置 schema、关联 debug 服务及 DAP 版本／能力，按 D16 接原 Debug 服务；不开放任意 VS Code DebugConfigurationProvider API 或包提供的 UI 脚本。`skills` 是 D18 的声明式资源贡献，接原 Skill owner，不等同运行期工具注册。

命令可选 enablement 沿原 Command 的 UI precondition；菜单项 when 只控制该位置的呈现，快捷键的有效条件是命令 precondition 与自身 when 的合取，用户键位覆盖继续沿原解析规则。命令面板、菜单和快捷键使用原可用性处理；程序化命令调用不把 enablement 或菜单可见性当作业务授权，仍核实际目标、业务权限、当前插件／handler 状态及参数。缺省条件表示无额外 UI 条件；已填写但词法／语法非法的表达式必须在发布前拒绝，不将解析失败的 undefined 当作缺省。解析成功后仍核其引用键是否属于产品已公布集合，不能以可解析代表可使用。

平台键位在消费端 Workbench 按客户端 OS／键盘映射解释，保留各平台声明和原用户覆盖；目标 Gateway OS 只决定插件运行兼容性，不能在目标安装／发布时按目标平台裁掉客户端键位。

同一插件绑定 v1 只有一个 runtime，插件内命令处理者及服务共享其生命周期；同包兄弟插件各自运行。纯菜单／快捷键／配置／Skill 插件无需入口。贡献归属同一插件开关及固定范围，不设置另一套贡献 enabled；声明不能推断成跨工作区业务授权。

扩展点目录由产品维护种类、合同版本、静态／运行期属性、原服务接缝及支持状态；安装时核对 `requires` 与实际支持集合，声明未开放种类或不兼容版本明确拒绝，不能静默丢掉一部分贡献后报告可用。完整架构不要求首版为每个点编写 Provider、SDK 方法或假实现；未来每开放一个点，再补它的数据／操作／事件 schema、原业务 owner、能力边界及真实纵向验收。

### D13. 注册身份、命令调用与 Provider 公共边界

逻辑贡献 ID 包含 package_id、plugin_id 和稳定局部 ID，注册归属包含原生 Gateway、必要 workspace、包确切版本和插件启用绑定。运行期注册另带 instance_id、registration_id 和原通道代际；这些是生命周期句柄，不作为业务 URI。Workbench 内同一逻辑命令只登记一个原 Command dispatcher，按发起编辑器／视图捕获的明确目标选择合法绑定，不为 A/B 安装相同 ID 而互相覆盖、不按最后注册者路由。

执行 dispatcher 唯一不代表 UI 元数据可以全局覆盖。标题／分类、命令 precondition、菜单 when 和默认键位按发起编辑器／视图的 scoped context 与合法目标版本投影；系统补入范围及实际可用性条件，不能由包放宽。命令面板沿其打开时捕获的业务上下文，未明确目标沿既有选择流程；实际接受前仍核原绑定有效，不能因焦点／版本变化换投。需要显式处理原 MenuRegistry 的隐式 CommandPalette 条目，避免隐式项绕过目标条件；不逐目标覆盖同 ID 的全局元数据或用最后注册目标的 precondition 生成所有键位。释放一个目标只结清其投影，保留其他绑定和唯一 dispatcher，继续复用原 Menu／ContextKey／Keybinding 服务，不建第二命令目录。

注册需已启用声明、已协商扩展点和实际授权；同绑定重复 ID、跨插件冒名或试图覆盖内置 ID 在发布前拒绝。每项注册返回可释放句柄，归原生命周期容器持有。声明校验和局部注册失败需撤销本次新增注册，不宣称分布式事务或回滚已有业务副作用。编辑器／设置等未开放点不因有占位描述而参加原 resolver／查询。

同一逻辑贡献 ID 的包版本或声明内容变化必须更新，不能只比较 ID。原 owner 先校验并构建完整候选声明，成功后才释放本绑定旧句柄并按固定顺序登记新句柄。校验失败不动原发布；登记阶段意外失败只清理本次新增句柄，标记本绑定贡献发布失败并停止后续发布，不尝试重建已释放旧句柄。未受影响的 owner／目标保持原结果；残留旧版本按 D7 仅作诊断，同绑定新业务准入不伪报成功。只使用原 DisposableStore 等容器，不建立通用注册事务、回滚栈或补偿操作。

客户端只消费目标、版本和原注册代际标识的完整声明快照：相同快照不重装，变化时替换对应绑定的原服务句柄，关闭时释放对应句柄。标题／条件／键位／schema 从该快照和原 scoped context 派生，不分别保存可独立修改的镜像或引入代理对象体系；原 Menu／ContextKey／Keybinding／Configuration 继续解释和执行规则。移除 A 不清空 B，同逻辑命令仍有合法绑定时保持唯一 dispatcher；无逐字段同步协议、全局 clear 或普通交互前远端发现。

目标初次装配向原 owner 交付完整当前快照，处理者晚注册也须消费当前状态，不仅订阅未来增量。首批固定先命令、后引用它的菜单／快捷键；菜单仍只使用产品已公布位置，不额外开放插件自建 submenu。配置由原 owner 在对应目标设置发现前发布；运行调用仍等待真实 ready 与 handler 注册，不借静态装配宣称可执行。不引入可配置依赖图或通用拓扑器。

绑定／实例管理结果逐项包含原生 Gateway、scope、必要 workspace_id、package_id／plugin_id、确切包版本及适用 instance_id，安装库存不强求不存在的绑定。schema 结果同样保留目标与包版本，原配置 owner 在冻结的设置上下文提供投影；A/B 同插件不同版本的字段和默认值不得压平成 Workbench 单例全局 schema 互相覆盖，不另设第二配置 registry。

命令输入为冻结目标、声明 command_id、合法参数、调用身份和剩余期限；结果、错误、进度／取消沿已登记合同和原 request_id 返回。由前端或 Backend 经 Gateway 到精确 runtime，目标原生 Gateway 核准入，业务权限仍由对应 owner 核验。菜单 when、快捷键和快捷入口只控制交互，不授予访问权；用户命令不会自动成为 Agent 工具，MCP 工具仍归原工作区 MCP owner。

运行期 Provider 共用注册／释放、目标、取消、事件与失效规则，具体数据和方法按扩展点定义，禁止通用 `invoke(method)`／任意 payload 转发替代 typed 合同。运行期支持集合只能缩到已启用 manifest 的声明；实例声称未声明能力时拒绝。首批命令注册验证真实具名处理者后才可执行，不能将所有命令交给一个无限制执行入口。

### D14. 开关驱动的注册、事件与释放

启用插件发布其声明，代码插件由 Gateway 按 D5 启动，真实 ready 及 handler／目录注册完成后才提供执行。未开启、启动中、失败的命令／工具明确不可执行，不为请求等待重新启动或扩大调用者管理权限。纯声明插件只发布静态贡献。包声明／配置 schema／诊断可读不代表所有贡献已启用。

事件复用原 SSE／WebSocket 及订阅 owner，按插件绑定、instance_id、registration_id 和通道代际隔离；断连失效对应执行注册，旧事件不污染兄弟或新实例。原实例重连仅读取真实快照、在合法代际重新注册，不重放命令；新实例不复活旧句柄。用户关闭撤销该插件的新业务准入、监听和运行注册；更新绑定、工作区解除、撤权、卸载按对应范围释放，关闭窗口只释放该窗口投影，不改 enabled 或停止共享插件。

卸载仍以整包为单位按 D7 核全部插件的四类引用；先通过用户明确关闭／解除绑定结清，关闭但未解除仍算绑定引用，不能因卸载强杀其他工作。可释放 UI 投影在准入处结清，不能把自身投影做成永久阻碍。停用保留已安装版本的合法 schema／诊断读取；真正卸载才释放对应 schema 投影，其他目标及仍使用的版本不受影响。移除贡献不删除配置、快捷键或业务历史，Skill 冻结正文按原 owner 保留；卸载参数残留仅按 D9 原登记容器编辑和删除，其他字段仍按原 schema 校验，不依赖已删除包提供表单。不新增按需激活器、全局轮询、通用 RPC 或第二注册框架。

### D15. 现有注册接缝与开放状态盘点

下表基于 2026-11-08 共享源码检视，表示可复用基础，不代替真实浏览器或自有插件验收。首批“开放”是本 change 的实施目标，当前自有贡献接入均待完成。

| 扩展点 | 已有机制／项目实现与源码接缝（`src/clients/workbench/` 下） | 自有插件范围 |
|---|---|---|
| 命令 | `src/resourceActions.ts`、`composerActions.ts`、`automation/actions.ts` 已用 Action2／Commands | 首批，补声明与具名 handler |
| 菜单／when／快捷键 | 原 Menu／ContextKey／Keybinding；项目已使用编辑器菜单与条件 | 首批，补合法位置／上下文目录及声明接入 |
| 配置 | `src/settings/`、`src/theme.ts` 已接原设置／配置服务；旧目标接线仍待迁 | 首批，补自有 schema 并保持配置域 owner |
| Skill 资源与工具说明 | Backend `app/agents/skill_catalog.py`、`tools/skill_loading.py` 及原资源／上下文 owner；不属于 Workbench 专有扩展点 | 首批 plugins[].contributes.skills，复用已有目录／读取／激活，不新建技能管理器 |
| 受管服务 | Terminal／Browser／MCP、Node 调试及原进程原语已有 | 首批，统一托管上述服务；不代表通用 Terminal Provider API 已完成 |
| Quick Access | `src/search/nameModes.ts`、`workspaceQuickAccess.ts`、`resources/quickAccess.ts` 已注册 | 未开放插件查询 Provider；首批命令沿原 `>` 可见 |
| View／ViewContainer | `src/agent.contribution.ts` 已注册导航／底部面板 | 未开放；会话树是 ViewPane 中 React，并非通用 TreeDataProvider |
| EditorPane／Resolver／Serializer | `src/thread.contribution.ts`、`automation/contribution.ts` 已接入 | 未开放第三方编辑器；保留 Thread、预览固定、分屏及恢复 |
| FileSystem／文本内容／Opener | `src/workspaceFiles.ts`、`sourceDocuments.ts`、`changeFiles.ts`、`resourceOpener.ts` 已接入 | 未开放；现有文件 Provider 只支持其合同，删除／重命名仍明确拒绝 |
| 文件／正文搜索 | `src/search/searchService.ts` 已注册原 file／text Provider 并接联邦搜索 | 未开放插件搜索，原 owner 查询保持 |
| Outline／符号 | `src/search/threadOutline.ts` 已接 Thread 大纲；workspace symbols 仍依赖旧宿主 Provider | 未开放；删除旧宿主前清点全部符号消费者，不用移除有效业务能力代替迁移 |
| 语言能力／Diagnostics | 原语言设施及 `scripts/extensions/extensionLanguageFeaturesTransform.mjs` 的旧目标接线 | 未开放；不能把旧 RPC 成果当新协议完成 |
| Debug | 原 Debug 服务／UI；Backend `node_debug` 已有 Node Inspector、20 个源码调试工具 | 首批自有 `debuggers` 与 DAP，迁移真实 Node 业务；旧 vscode.debug API 仍删除，Agent 执行调试另属 Backend |
| Terminal／Task Provider | 产品终端／持续任务可用；旧目标 API 在各 `extension*Transform.mjs` 明确拒绝 | 未开放通用 Provider；持续任务调度及条件脚本执行保持原 Backend 实现 |
| Notebook | 原设施存在，`extensionNotebookTransform.mjs` 对目标扩展明确拒绝 | 未开放 serializer／cell／kernel／renderer |
| Webview／Custom Editor | 原设施及资源转换存在，完整隔离与自有消息合同仍需验收 | 未开放任意插件 UI 脚本 |
| 主题／图标／语言声明／语法／片段／JSON 校验／本地化 | 原静态贡献机制存在，项目颜色见 `src/theme.ts` | 未开放下载包贡献；保留产品内置能力 |
| SCM／状态栏／认证／测试 Provider／Chat 扩展 | 原服务或扩展点存在，未证明项目自有注册链完整 | 未开放；项目聊天不自动成为 Chat Participant，秘密不交新 owner |

本表连同 capability 目录维护“基础设施存在／项目业务已接入／自有插件开放／真实验收”四种独立事实。未来扩展沿同一注册合同新增，不新建第二套菜单、设置、文件系统、搜索或扩展宿主。

### D16. 源码调试迁移与原版 Debug UI

当前 `app/agents/policy/tool_groups.py` 的 `debugging` 组已有20个源码调试工具，注册在 `app/agents/tools/debugging/registration.py`；`app/services/infrastructure/node_debug/` 已实现 Node Inspector JavaScript 调试，Backend 在 `process/launch_orchestrator.py` 直接创建目标进程。本切片迁移这条真实链，不把已有调试误记为只有上游占位。首批自带 workspace 范围 debug 服务插件，持有 adapter／Inspector 连接及调试目标进程；服务实例可管理多个有独立身份的调试 run，不按每个 Thread 创建管理器。Python 是插件可用实现语言，不代表首批支持 Python 源码调试。

Backend 原调试 owner 保留配置、活动方案、断点、动作时间线、精确 `(session_id, thread_id)` 归属、准入、脱敏和 canonical 工具审计。Session-only 入口仍从 catalog 解析真实 main_thread_id，不能用 `main` 别名。原 `/debug/node` API 及20个工具的公开输入输出／call ID 保持；不另建 launch.json 配置权威或复制调试业务状态。Agent 执行本身的暂停、断点、停止快照继续在原 Backend owner，不混为源码调试。

Workbench 复用原 Debug 服务、视图、编辑器断点、调用栈、变量和控制台，经自有贡献注册到同一业务入口：UI → Gateway → Backend 调试 owner → Gateway → 精确 debug 插件。Backend 的 DAP 接入复用原调试操作，维护必要的协议映射；Gateway 只转发，不解释断点或求值。Agent 工具和 UI 共用同一 run，人类操作无需交接到第二个调试器；从同一暂停状态发起的互斥操作由原 owner 串定结果，旧暂停代际请求明确失效。DAP 路由保留原目标和关联 request_id，不能借 UI 绕过 Backend 准入或工具调用资格。

同逻辑调试类型沿 D13 的插件贡献命名空间，只在原 Debug 服务登记一个 factory；创建 adapter 从冻结的调试 session／run 目标、插件绑定和版本派发到原 Backend owner，不按最近注册者或当前焦点选目标。调试器标签及配置 schema 在对应目标上下文解释，不按相同 type 合并不同目标版本的字段或全局覆盖 factory；解除 A 的绑定不删除 B 仍使用的 factory／投影，最后合法引用释放后才结清共享注册。不伪造 IExtensionDescription，也不增加调试运行或配置权威来源。

目标及插件绑定版本在调试发起、首次异步配置解析前冻结；无明确目标先沿原选择流程结清。原启动上下文贯穿配置解析、变量替换、session 创建、adapter 分发及已支持的 runInTerminal 等 factory 入口；session 尚不存在时传原启动上下文，不凭 folder、同名配置或稍后焦点推断目标。参数按原目标 owner 的已支持规则解释；绑定失效／版本变化明确拒绝，不先用另一目标替换变量再把 adapter 路由回来，也不开放任意用户配置 Provider。

DAP 合同覆盖 initialize／能力协商、launch 与 configurationDone、源码／断点（含 logpoint／验证结果）、threads／stackTrace／scopes／variables、continue／next／stepIn／stepOut／pause、evaluate，以及 output／stopped／continued／terminated／exited、错误与取消。按 DAP 规则处理初始化时序及 setBreakpoints 的按源替换语义，映射到同一断点 owner；不复制 Agent 与 UI 断点集。只声明已实现请求／选项；attach、restart、条件断点等逐项按现有实现验证，未实现明确拒绝，不能因 DAP 支持该方法就报告可用。源码读取／跳转沿既有 URI／虚拟文件系统和源码版本，远端路径不冒充本地 file URI；sourceReference／frameId／variablesReference 只是会话句柄，按 run 及暂停代际失效。

服务 instance_id、调试 run_id、暂停代际分别记录；旧栈帧／变量引用不能作用于继续后或重启后的 run。断流不等于目标退出，Backend 重启仅按原真实 run 查询／附加能力恢复可观察状态，不假报旧工具调用恢复。launch／continue／evaluate 等已发送操作结果未知时不重放；求值副作用和脱敏遵守原审计。disconnect 与 terminate 分别处理，单纯关闭 UI 不自动杀目标；显式 stop／restart 核实本服务拥有的 launch 目标及子进程退出，attach 外部进程不能当作自有进程强杀。若协商支持 runInTerminal，只能经同一 Terminal 插件和精确 run 归属创建目标，不由 renderer／Backend spawn；首批未实现时不宣称支持。

验收用实际 Node 脚本，经真实 Gateway 在原 Debug UI 启动、断点停住、单步、读取变量，再由 Agent 操作同一 run；核双 Thread 隔离、继续后旧引用拒绝、响应丢失不重放有副作用求值、停止后实际退出。不以 DAP mock、20工具注册存在或旧 VSIX 调试 API 的拒绝测试代替产品验收。

### D17. 工具族清点与统一执行边界

实际静态分组是 `default`、`agent-collaboration`、`debugging`，不是按每个服务拆组。以下依据 `agent_tools.py`、`builtin_tool_registry.py`、`workspace_filesystem_tools.py`、各工具注册和外部进程创建点盘点；MCP／custom 的动态集合另按原目录 owner 的运行快照核对，不能凭静态名单宣称覆盖所有未来工具。迁移不改变公开 group_id、工具名、输入输出、封存资格、schema hash、call ID 或结果归属；“扩展工具”也不等于旧 VS Code 扩展，删除旧宿主不能误删这些注册。

| 工具族／执行链 | 权威业务 owner 与执行归属 | 本 change 范围 |
|---|---|---|
| exec_command／write_stdin／list_terminal_sessions／kill_terminal | Backend 原工具／资源 owner；Terminal 插件持有 PTY | 已纳入，迁齐全部间接消费者 |
| 浏览器工具及 Playwright | 原资源／协作 owner；Browser 插件持有浏览器 | 已纳入，保持资源和锁合同 |
| MCP／外部 custom 工具 | Backend 原发现、封存、资格与结果 owner；具名 MCP／typed 服务负责外部执行 | 已纳入；custom 实现不得保留 Backend spawn 旁路，不强改工具为 MCP |
| debugging 的20个 Node 调试工具 | Backend 原调试 owner；workspace Debug 插件 | 本次补入，见 D16 |
| python_exec | 原 Backend Python 工具及 subprocess 执行 | 用户明确保留，调用、解释器选择、执行位置和清理均不改 |
| 持续任务 script 条件 | Backend 调度／判断／TaskDecisionDTO 及 native_command 监督执行 | 用户明确保留，原脚本调用、进程监督及限额机制均不改 |
| 视频附件 ffmpeg | Backend 附件／内容 owner 及原 subprocess 执行 | 用户明确保留，原抽帧、临时文件和错误处理均不改 |
| Bun／uv 安装准备 | 唯一安装 owner；同一受管执行原语 | 已纳入，不能与插件首次调用混在一起 |
| apply_patch、ls／read／glob／grep／write／edit、资源／上下文搜索 | 原文件／文本／搜索 owner | 保留，URI／版本／CAS 不移交 Gateway，不为纯业务计算创建插件 |
| Goal／任务、子 Agent／团队／消息／等待、时间与后台消息、Agent 执行调试 | Backend 原编排、会话和事件 owner | 保留，包括原条件脚本执行；builtin 条件和模型 judge 不另插件化 |
| web_search／fetch_webpage、skill_load | 原 HTTP 查询／技能 owner | 保留现有无需子进程的实现；注册名不构成服务进程需求 |
| connections.manage、测试工具 | Gateway 原控制面／测试装具 | 不新建业务插件，不将测试工具打包为常驻服务 |

用户明确要求 python_exec、持续任务条件脚本、视频预处理 ffmpeg 保留原用法：三者均保持现有 Backend 执行位置、调用协议、进程管理和清理，不经过 Gateway，不接终端／执行插件，不增加适配层或备用分支。python_exec 的解释器选择、cwd／PYTHONPATH、双流、非零／超时错误和临时脚本清理保持；条件脚本的 JSON 输入、原监督器及期限／内存／输出保护、TaskDecisionDTO 验证和续跑语义保持；ffmpeg 的抽帧参数、临时文件及错误处理保持。本 change 不为这三条链新增非 PTY 执行服务或修改其既有测试。

安装准备及明确纳入的外部 custom 插件执行仍遵守既定托管边界；不借工具盘点把其他 Backend 子进程自动归为待删除旁路。Gateway 不新增任意 shell／进程 API，业务准入继续由原 owner 负责。

另外发现两类非工具进程，明确留出边界：SQLite fork 快照的隔离子进程是存储 owner 的内部一致性原语，本次保留，不将数据库／物理会话路径交 Gateway；系统文件管理器显示的 `open`／explorer／gio／xdg-open 是宿主动作，暂不迁成远端插件，也不因此给 Electron 新增原生能力，现有行为不在本次顺带删除。两者后续如改动需独立设计和验收。Launcher／SSH／Gateway／Workspace 生命周期继续归原控制面；async task 的 spawn 不当作 OS 进程。完成标准是上述工具及外围执行单链迁齐，不是“Backend 绝无任何子进程”。

### D18. 插件随附 Skill、工具关联与版本生命周期

首批增加 `plugins[].contributes.skills` 声明数组。每项含插件内稳定 `id`、包内相对 Skill 目录 `path` 和可选 `tool_bindings`；目录必须包含 SKILL.md，配套文档、示例和脚本沿该目录读取。名称／说明由 SKILL.md 的现有 frontmatter parser 唯一派生，不在 manifest 重复维护；使用现有命名、正文及大小约束，缺文件、越出包根或同一来源重复名称在发布前明确拒绝。`tool_bindings` 每项引用稳定 tool_id、明确服务贡献（服务工具时）及支持的工具合同版本，复用原目录版本／schema hash 标识，不另建工具版本登记；只引用本插件服务导出或产品明确允许关联的工具，不以显示名、端口或任意 factory 表达关系。安装只做声明／包内引用检查，不启动服务、等待 MCP 目录或远程探测全部工具；运行期关联可用性由原目录发布状态决定。纯 Skill 插件无需 runtime，工具关联缺省表示独立说明资源，不凭关联创建工具。

包仍由目标原生 Gateway 唯一安装 owner 持有不可变版本；仅明确开启的插件贡献进入该绑定可见的原 Gateway Skill 来源，Backend 经现有认证来源读取并由原 SkillCatalog 发布。沿 workspace > gateway > network > inline 四层优先级，不新增第五层、另一来源 registry、技能索引或向每个工作区复制文件。同层插件贡献名称冲突按原 owner 明确拒绝候选并保留既有发布，不按安装顺序覆盖或临时改名。贡献可见范围由插件固定 scope、精确启用绑定和原资源权限共同决定，不能仅凭进程存活推断；workspace 绑定的 Skill 不披露给另一工作区，经 Relay 读取仍来自原生目标，不复制到中转 Gateway。来源保留 package_id、plugin_id、包版本／摘要、贡献 ID、原生目标及原资源 revision；Skill 展示及配套文件读／列举／搜索使用原寻址 owner 的合法技能 URI，不暴露物理安装路径、恢复 VSIX 包根或把实例代际写进 Skill URI。扩展来源装配必须按原寻址合同登记，不能由插件拼 URI。

工具发现／调用继续走原 search_tools／invoke_extension_tool 信封和唯一封存目录，名称、参数 schema、group_id、call ID 不因执行位置迁移而变化。包的 tool_bindings 是包内默认说明关联，由原工具目录 owner 与 tools.custom[].skills 显式关联归并、按解析后的来源身份去重；不回写用户配置，不维护第二工具目录。关联只用于说明、发现和归因，不启用工具、不加载 Skill，也不授予业务或运行管理权限。现有 SKILL.md 的 allowed-tools 不进入 Skill 授权或工具注册合同，不据此生成 allowlist；不能以 Skill 内容绕过工具策略。工具尚未发现、已失效或合同版本不兼容时，关联显示明确原因，不按同名工具／新实例猜测匹配；读取 Skill 本身仍可用于诊断。同名 Skill 被更高来源覆盖时保留真实来源，不把覆盖内容假称来自包；匹配合同必须核实际来源及版本，不能只核名称。

安装、插件开启、读取资源、skill_load 与原工具策略是不同操作；贡献没有另一个启用开关，代码启动是插件开启的结果。包发布本身不使 Skill 对所有工作区可见；启用后只发布合法目录。skill_load 仍只接名称及原 snapshot／tracked／untrack 模式，从本次冻结的 Turn／ModelCall activation snapshot 解析，普通文件读取不激活、不追踪，加载正文不启动插件或执行包内脚本。元数据仅公布名称／说明，正文仍由原 ContextSourceManager 注入和封存；frontmatter 其他字段不重新获得上下文语义。展示说明不以服务 ready 为前提，也不在每次模型准备或技能加载时访问公网、探活进程、hash 全包或刷新全部工具目录。

更新安装 v2 不改运行／贡献绑定 v1；明确切换插件绑定的包版本后才由原来源 owner 发布新 revision，包与 Skill／工具合同关系同一候选校验后可见，不宣称跨进程事务。已冻结调用继续使用其原 Skill 正文、来源与工具资格，不在运行中改用 v2；snapshot 不自动跟新，tracked 仅沿原观察／接受机制发现变化并按原上下文时序生效，untrack 不删除已注入正文。缺版本或冻结冲突沿原 owner 明确报错，不能回退同名当前包。插件整体切换包版本时，有代码插件先核实旧实例退出再启动新版本，静态说明与该插件包版本一致；不单独升级贡献或宣称旧运行版本尚无的能力。

停用、撤权、工作区解除绑定及卸载撤销对应的新发现／新读取／新加载准入及观察订阅，不删除原会话已封存的 Skill 正文、来源、工具历史和用户配置，不改写既有 snapshot／tracked 语义。进行中的资源读取引用按原规则结清；卸载仍检查真实包／运行引用，拒绝时显示实际引用。原上下文已持有完整冻结正文时不另永久占用包引用；仍需包文件的合法未完成读取保留真实引用，不新建持久参与者账本，也不强行删除正被使用的版本。插件关闭同时停止其 runtime 和新 Skill 使用准入；崩溃／重启只是运行状态变化，不自动关闭已启用的静态说明，无运行管理权限也可在原资源披露权限允许时读取已开启插件的说明。

| 当前产品 Skill | 迁移决策 |
|---|---|
| browser-control | 随 Browser 插件迁齐正文、配套文件、工具 schema／示例及声明；切换后移除原 inline 同义发布与 bundled group／打包引用，保持 name 和工具调用信封 |
| debugging | 随 Debug 插件及20个工具纵向迁移，同上处理；Agent 执行暂停／快照仍属原 Backend，不借 Skill 改业务 |
| web-search-fetch | 保留现有产品资源和 Backend HTTP 工具，不因新增 Skill 贡献改成服务 |
| gateway-context | 保留现有产品资源及控制面／查询 owner，不包装成工具执行进程 |

迁移前全仓核 bundled_skill_groups、tools.custom[].skills、Skill URI、模型提示、示例、前端 skill_names 展示、测试和发行消费者；发布切换后不保留 browser-control／debugging 的旧 inline 同义来源作为 fallback，其他合法 workspace 覆盖仍沿原规则。历史冻结来源由原资源／上下文 owner 处理，不为保旧开发路径留下双读。python_exec／持续任务条件脚本／ffmpeg 的执行和原 Skill 关系仍不改。

验收集中为少量真实入口场景：无进程纯 Skill 插件；Browser／Debug 经 skill_load 后用原信封操作；v1 冻结／v2 显式切换、tracked／untrack；两工作区与跨 Gateway 来源隔离；停用／撤权／有引用卸载；高层同名覆盖及不兼容工具关联。使用原资源、工具和上下文装具，失败不默认跑全套；普通目录／读取遵守原暖请求总预算，模型准备／skill_load 不附带新的远程发现或全量自检。

### D19. 当前范围开关与新建工作区默认值

Gateway 配置 owner 持有非秘密的新建工作区插件模板（workspace_defaults），记录明确 package_id／plugin_id、确切包版本和 enabled。只接受 workspace 范围插件，不存工作区凭据／参数、不创建实例；每个原生 Gateway 独立管理，Relay 不设本机副本。不复用用户级 Workspace fan-out 作为模板，已有工作区不追随模板变化。

工作区创建 owner 在创建 intent 中冻结当时模板 revision／条目，初始化该新工作区的独立配置，再经原 Workspace writer 及认证服务绑定交付 Gateway；Gateway 不直接读写 .boxteam。原创建重试复用冻结输入，不换读最新模板；模板创建默认值不构成第二工作区权威账本。注册／挂接已有工作区不应用模板或覆盖原配置。缺失包／插件／不支持范围在模板提交前明确拒绝；模板及已冻结未结清创建 intent 的包版本均计入原安装 owner 的真实使用引用；更新清理和卸载不得移除这些版本。模板修改不释放旧 intent 引用，创建完成交接为工作区绑定引用，明确取消／失败结清才释放；不另建引用账本。卸载须先由用户解除实际引用，不悄悄删除默认值。安装不自动启用，也不自动把插件加入模板；内置默认值由发行配置显式提供。

引用只从原配置的 active／pending、Gateway 模板、原创建 intent、原 runtime 和读取 lease 查询，不派生持久引用图或按历史正文反查。模板提交、创建冻结／交接和版本删除复用安装 owner 的原互斥边界，完成交接后由工作区绑定接替 intent 引用，不能留下“两边都等待对方释放”的循环；未结清即显示冲突，不增加定期修复任务。工作区绑定交付只由原启动／配置变更通知或用户显式重试触发，缺输入显示等待、认证失败显示失败，普通状态读取不触发重交付；已核实进程保持原接管规则，不扫盘或自动修配置。

纯声明插件开关也沿上述两个运行范围绑定管理，无进程状态。新建工作区继承后已有自己的 enabled 和包版本；编辑当前工作区开关不改模板，编辑模板不改当前／其他已有工作区。UI 分开显示“仅此工作区”和“以后新建工作区”，批量跨范围操作逐项报告，不把默认值保存成功当作插件已运行。

### D20. 最小调用检查与成本边界

安装／启用／配置变化时校验包、插件合同、范围和管理准入；注册／目录变化时由原工具 owner 校验 schema 和生成不可变身份。暖调用只核当前工具是否禁用、参数、冻结目标／有效注册与剩余期限，保留必要的撤权和原 call ID 结算。相同 schema 的导出／hash 在注册版本变化时计算，不逐调用重做；同一合同不跨层重复解释和校验。Gateway 核认证、精确插件／实例路由和范围，Backend 核业务，插件执行具名协议，不各自建立一套完整授权引擎。参数在不可信输入进入合同的必要边界验证，不能以简化取消必要目标／版本隔离。

普通调用不查询进程列表、不反复探活、不重新读取全部配置／Skill／工具目录；进程状态沿已有句柄与事件。安装和开关权限不要求普通业务调用者持有，插件关闭／未 ready 直接返回真实状态，零隐式启动。全部开关／命令／状态计时仍从真实入口至结果，遵守原暖交互总预算；不为此增加审计／统计框架。

审批不属于本次插件能力或管理协议。沿原执行 owner 保留已经显式配置的可选人工确认，仅作为受影响调用的回归验证；普通会话和插件管理不新增审批请求、入口、等待状态或轮询。安装／启停／配置权限和 MCP OAuth 按原合同核验，不转换成人工工具审批。旧执行中断仍由原 checkpoint／控制 inbox／结果 owner 收口，本变更不新增审批存储、检查点修复、消息重组或执行续跑机制。

### D21. 插件包布局、产品源码与运行数据目录

目录按交付单位、实际业务 owner 和数据归属组织。插件包内部以插件聚合实现与贡献；产品只集中维护扩展点合同目录，实际注册靠近原服务；已安装内容、实例控制数据和工作区业务数据分别存放。目录名不成为发现协议，不按文件布局推断插件身份或启用状态。

插件包推荐布局如下，示例入口语言不限制对应服务的实现语言：

```text
开发工具集/
├─ plugin.jsonc                     # package_id、version 和 plugins 声明
├─ plugins/
│  ├─ browser/
│  │  ├─ main.py                    # manifest 声明的入口
│  │  ├─ skills/
│  │  │  └─ browser-control/
│  │  │     ├─ SKILL.md
│  │  │     └─ examples/
│  │  └─ …                          # 浏览器实现及资源
│  ├─ terminal/
│  │  ├─ main.mjs
│  │  └─ …
│  └─ debugging/
│     ├─ main.mjs
│     ├─ skills/
│     └─ …
└─ …                                # 依赖声明、锁文件及共享资源
```

根 plugin.jsonc 是唯一包清单，plugins[].id、entrypoint 和各贡献路径决定逻辑插件及资源，不强制内部目录叫 plugins、browser 或 main。manifest 内入口与资源路径显式相对包根解析；Skill 正文的相对配套引用沿原 Skill 目录规则。纯声明插件不要求入口。包内可以共享纯代码／静态资源，但不因此共享运行实例、可变状态或凭据，也不引用兄弟插件私有 handler。自带包、下载包和显式开发目录使用同一结构与合同；开发修改需显式重载，安装或读取不递归扫描目录来猜测新插件／Skill，不生成每个插件的第二清单。

安装发布及开发目录显式重载核物理路径边界：归档条目、解析后的入口及贡献资源不能越出包根；绝对路径、越界穿越或指向根外的链接在写出／发布前拒绝。包含关系不能只比字符串前缀，不把这项安装校验移到每次工具／Skill 读取。

产品源码落点如下；新增目录只在相应切片有实际实现时建立，不提前铺空框架：

| 目录 | 职责与复用边界 |
|---|---|
| proto/boxteam/ | 管理、注册及具名调用的跨语言合同；沿现有 owner 和生成链，不手写第二套 Python／TypeScript 协议 |
| app/gateway/plugins/（新增） | 包解析、产品扩展点支持目录、安装发布和插件绑定协调；复用原配置、安装记录及进程 owner，不另建 supervisor 或业务目录 |
| app/gateway/runtime/（现有） | 实际启动、接管、退出核实和日志归属；插件管理调用原机制 |
| app/services/、app/agents/ 的原业务模块 | 保留 MCP 连接／工具目录、SkillCatalog、调试准入、资源及会话历史等业务职责，不将其搬入 Gateway 插件管理目录 |
| src/clients/workbench/src/extensions/（现有） | 包／插件管理 UI、目标绑定、声明投影及工作台贡献装配；直接接原 Registry |
| Workbench 原 settings、调试及其他消费模块 | 各自扩展点的实际注册／释放和交互，继续使用原 Command、Menu、Configuration、Debug 等服务 |

扩展点目录只集中定义标识、合同版本、支持状态及 D15 的接缝元数据；各扩展点的具体校验、发布和注销仍由实际 owner 执行，不在 Gateway 再造命令表、SkillCatalog、配置或调试业务系统。上述是对迁移后唯一落点的约束，旧安装／宿主消费者仍须按 D10 逐链迁齐再物理删除，不增加旧代码适配层。仓库内新增源码子目录补齐 AGENTS.md 四节；不对第三方包额外要求仓库协作文件。

安装内容由原生目标唯一安装 owner 在既有安装域内按 package_id、确切版本及摘要管理，全局安装数据归 ${BOXTEAM_HOME:-~/.boxteams}/，不另设前端安装副本或工作区包副本。依赖在发布前准备，发布后的包内容只读；启动、Skill 加载及工具调用不向包内写日志、运行缓存、配置或秘密。实例控制数据复用 Gateway 原 runtime／控制面目录，诊断日志沿原 state/logs；工作区参数、私有输入、浏览器／终端业务资源及会话产物仍由各原 owner 写入其合法工作区数据目录。包内 Skill 注册只读资源路径并通过原资源链读取，不复制到可编辑工作区；用户已有的合法工作区 Skill 覆盖仍按 D18 生效。这里不新增运行账本、业务 URI 模板或通用用户数据目录。

实例工作目录和可写输出指向对应原 owner 合法目录，包内容是不可变输入；“只读发布”是包及运行实现的约定，不宣称 Python／Node 子进程受到安全沙箱强制隔离，不新增沙箱或逐请求文件完整性检测。

验收复用原安装、贡献和真实产品切片：改变包内子目录且同步 manifest 后仍可解析入口／Skill；纯声明插件零进程，缺文件或越界路径明确拒绝；安装后真实加载 Skill 并调用工具，重启插件后核安装内容未被运行写入、业务数据及日志仍归原 owner。包校验沿安装发布边界，验收不引入逐请求全包 hash 或递归自检。

### D22. VS Code 源码对照与明确差异

本轮对照 reference_repo/vscode 的提交 fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca，只检索源码，不运行参考仓库测试或修改上游。以下路径均相对该参考仓库：

| 已核实的上游接缝 | 本项目采用的规则 |
|---|---|
| src/vs/workbench/services/extensions/common/extensionsRegistry.ts:76–167：按 identifier 计算 added／removed，handler 可晚于用户快照安装；menusExtensionPoint.ts:909–977、1044–1057 使用生命周期容器清理原贡献 | 采用快照／增删及释放思想，但内容和包版本变化必须显式替换；同 ID 不代表声明未变化，不照搬跨所有目标全量 clear |
| src/vs/workbench/services/actions/common/menusExtensionPoint.ts:871–872、954–963；src/vs/workbench/services/keybinding/browser/keybindingService.ts:623–657 | enablement 是 UI precondition，菜单 when 控制呈现，插件键位组合命令 precondition 与自身 when；程序化调用由业务 owner 判权 |
| src/vs/platform/contextkey/common/contextkey.ts:203–238、629–636、640–675：解析失败可返回 undefined，诊断 API 提供词法／语法错误 | 发布前区分未填写与非法条件，复用原 parser／诊断并核公布键集合；非法条件不能放宽成无条件 |
| src/vs/workbench/services/extensions/common/extensionsRegistry.ts:109–167、663–695；abstractExtensionService.ts:1167–1175；menusExtensionPoint.ts:1047–1051；keybindingService.ts:130–134 | 原扩展点保存快照并可晚装 handler；descriptor 的 deps 并不提供通用拓扑调度。项目只规定已开放贡献的固定最小引用顺序，不增加依赖调度引擎或借此开放新贡献种类 |
| src/vs/workbench/api/common/configurationExtensionPoint.ts:262–274、357–374；src/vs/platform/configuration/common/configurationRegistry.ts:505–520、744–765 | 复用原配置注册／释放思想；明确停用仍可读已安装 schema，卸载才释放对应投影但不删用户值。目标／版本隔离是本项目额外约束，不将远端业务 schema 混入全局单例 |
| src/vs/base/node/zip.ts:76–103；src/vs/platform/extensionResourceLoader/common/extensionResourceLoaderService.ts:34–43 | 借鉴安装路径及原资源读取边界；自有 ZIP 在安装处核物理包根，不能把示例字符串前缀检查当完备防逃逸方案，不复制 VSIX 下载器或新增运行沙箱 |
| src/vs/platform/actions/common/actions.ts:491–498、549–564；src/vs/workbench/contrib/quickaccess/browser/commandsQuickAccess.ts:227–260；src/vs/workbench/services/keybinding/browser/keybindingService.ts:638–646 | Command 执行与 Menu 元数据是不同注册；保留单 dispatcher，同时按 scoped context 隔离标题、条件及键位，防隐式命令面板项越过目标范围 |
| src/vs/workbench/api/browser/mainThreadSecretState.ts：以 extensionId／key 隔离；services/secrets/browser/secretStorageService.ts 默认内存、可注入 provider；platform/encryption/electron-main/encryptionMainService.ts 使用 Electron safeStorage | 借鉴普通配置与秘密分离，秘密归本项目目标 Gateway／Workspace 的原 owner；首版复用权限受限私有文件，明确不等同于上游系统加密存储，不恢复已退役的 context.secrets／Extension Host RPC |
| src/vs/workbench/contrib/mcp/common/mcpRegistry.ts 的 _updateStorageWithExpressionInputs、mcpRegistryInputStorage.ts；services/configurationResolver/browser/baseConfigurationResolverService.ts 的 promptString／password | 采用密码输入、保存后复用、更换及清除的交互；普通来源只存引用，秘密写入与来源 CAS 分开报告。上游 MCP 使用 AES-GCM 与秘密服务保存密钥，本项目不据此新增第二秘密系统；环境引用继续沿既有语法和明确目标解释 |
| src/vs/workbench/contrib/debug/browser/debugAdapterManager.ts:121–145、209–251 | 原 Debug 类型／schema／factory 具有全局接缝；同逻辑类型只登记一个目标分发 factory，声明按目标版本投影，不能直接逐目标覆盖原 Map |
| src/vs/workbench/contrib/debug/browser/debugService.ts:508–512、622–624；debugAdapterManager.ts:256–269 | 配置解析／变量替换先于 session 构造；目标及绑定版本从原启动上下文开始冻结，贯穿前置解析和后续 factory 调用 |
| src/vs/workbench/services/keybinding/browser/keybindingService.ts:606–626；src/vs/base/common/platform.ts:99–104、259 | 平台键位由客户端 Workbench 解释，远端安装平台不决定用户键盘规则 |

以上是行为依据，不要求复制原 ExtensionPoint 类型、IExtensionDescription、容错跳项或隐式激活器。项目采用 D12–D14 的自有合同与原注册服务，必要校验只在声明／配置／注册变化边界执行，不增加普通请求完整性扫描。

## Risks / Trade-offs

- [原 UI 与 VSIX 服务耦合] → 在原 UI 服务接缝绑定自有模型；不为复用 UI 保留旧对象／宿主，只复用可分离的组件与交互。
- [统一 Gateway 转发增加一跳] → 复用连接和流式转发，以真实总耗时验收，禁止每次调用重新发现／完整自检。
- [共享进程被误解为共享业务] → 服务登记与 workspace／资源归属分别验证，原资源 owner 继续隔离。
- [stdio 到 HTTP 桥能力不完整] → 公布实际 MCP capabilities，按声明验证双向请求／通知与取消；不宣称所有服务器能力自动可用。
- [崩溃时写结果未知] → 默认明确报错及不重放，后续专用恢复必须由对应服务另行设计。
- [把完整扩展架构做成全量 SDK] → 只实施首批七类贡献及必要公共合同，调试限真实能力，Skill 复用原 owner，未开放点不编写空 Provider／通用 RPC。
- [跨目标同 ID 与失效注册污染工作台] → 原 Command 单 dispatcher、明确插件目标和生命周期释放，旧实例回执不能改写新注册。

## Migration Plan

按 D10 的纵向链逐一迁齐和删旧，不规定所有模块全局串行。共享 schema／协议先定稿再迁消费者；独立服务切片可分别验证。代码回退不反向执行已提交的安装、命令或配置，数据恢复只使用原 owner 支持的显式流程。当前公网分发服务与高级生命周期扩展均不在本轮实现范围。
