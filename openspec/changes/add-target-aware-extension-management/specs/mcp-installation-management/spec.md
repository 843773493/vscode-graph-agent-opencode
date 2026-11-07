## Purpose

定义同一工作台中面向本地及远端后端目标的 MCP 服务发现、安装、版本与工作区使用管理，使用户明确区分安装制品、配置启用和实际连接，并由现有工作区 MCP owner 继续维护唯一 Agent 工具目录与调用事实。

## ADDED Requirements

### Requirement: MCP 目录必须与扩展市场区分并公布来源

系统 SHALL 在原工作台管理界面提供 MCP 目录查询、分页和详情，明确其来源及支持的运行方式；VSIX 市场与 MCP 目录 MUST 使用各自合同。未配置、不可达、不支持和完整无匹配 SHALL 可区分，不能因目录失败伪报没有服务。目录查询 MUST NOT 下载执行包、启动目标或隐式查询其它来源。

#### Scenario: MCP 目录不可达但扩展市场可用
- **WHEN** 用户打开 MCP 搜索，所选 MCP 来源不可达而 VSIX 市场正常
- **THEN** MCP 入口报告该来源失败，扩展市场保持独立，不把 VSIX 结果冒充 MCP 或显示完整无匹配

#### Scenario: 目录仅支持旧版协议
- **WHEN** 所选目录仅支持 `v0`，用户未显式设置协议版本
- **THEN** 默认按 `v0.1` 请求并报告原失败；不探测或自动改用 `v0`。用户明确选择 `v0` 后，仍沿同一 gallery 服务使用该协议

#### Scenario: 更换来源时旧分页尚未完成
- **WHEN** 原来源的首屏或续页尚在请求中，用户改变目录 endpoint 或协议版本
- **THEN** 原查询取消且原分页缓冲失效，旧成功与旧失败均不能改写新来源列表；当前来源的续页失败必须明确显示，不能伪报完整无匹配或留下未处理拒绝

### Requirement: 安装目标与工作区使用范围必须分别明确

MCP 管理视图 SHALL 在标题工具栏复用扩展管理的目标控件与交互规则，初次默认当前页面入口的本地 Gateway；视图持有自己的选择，列表、新详情与后续安装计划直接沿用，不因扩展视图切换或活动编辑器变化改投，也不在每个服务点击安装时重选目标。从既有对象发起时 SHALL 保留并展示对象目标。计划固定原生 Gateway、稳定后端目标、服务来源、精确版本及运行方式，不再要求选择一套 MCP 专用目标。只有启用或配置确实属于工作区时才 SHALL 确定精确 workspace 和来源层，已有上下文直接带入。浏览器运行位置、活动 Tab、同名目标和临时 route MUST NOT 代替上述身份。目标平台和运行依赖 SHALL 由目标侧实际能力判断；不具备能力时明确拒绝，不自动全局安装运行环境。

远端安装 SHALL 由远端原生 Gateway 授权并交其目标安装 owner 执行；直接访问该 Gateway 页面与经 Relay 管理 SHALL 读取同一制品和安装记录，当前页面不另建本地安装副本。统一目标入口 MUST NOT 将 MCP 和 VSIX 的不同协议／生态安装算法复制成一套通用插件平台。

来源身份 SHALL 区分 registry／endpoint、稳定 entry ID（只有名称时冻结名称及 manifest digest）、显式 package／remote variant、精确制品版本／摘要及安装域。同名不同源不得合并；更新须基于原安装来源和 manifest 基线重做计划，来源身份变化不能静默重绑。

#### Scenario: 选定远端后切换当前会话
- **WHEN** 用户在 MCP 视图标题工具栏选择远端 B，安装一个仅 B 平台可运行的服务，随后切到本地 A 会话并继续安装另一个服务
- **THEN** 两次安装直接沿用 MCP 视图已选 B，不逐项重选；安装只在 B 执行并报告 B 能力，本地 A 没有下载或进程；B 失效时明确失败，不改投 A

#### Scenario: 两来源同名同版本指向不同包
- **WHEN** 两个 registry 提供同名同版本、不同 digest 的包，用户安装其中一个，随后目录同名条目被替换
- **THEN** 两条目与安装保持独立身份，原安装不被改绑；更新展示来源变化并要求新计划，不按名称复用另一制品

### Requirement: 目录手动定义与 HTTP 服务必须进入同一管理链

系统 SHALL 支持目录服务、明确手动定义和 HTTP endpoint 经同一候选校验及工作区配置流程接入。含多个包／运行方式时 SHALL 明确解析选择及参数；不能无损表示的字段 MUST 报告不支持。HTTP 服务不需要本地包时 SHALL 表达为配置／连接，不伪报已下载。用户未明确启用时不得自动授予任意 Thread 工具或启动 turn。

计划 SHALL 绑定单一确切 variant；多个同类型候选、所选项消失、参数不支持或 manifest 基线变化时明确拒绝或重新选择，不能回退首项。当前 stdio／Streamable HTTP 支持范围必须真实公布，不把旧 SSE 冒充 Streamable HTTP。

候选 SHALL 从显式 endpoint／协议／entry URL 读取完整原件，不把 Gallery 展示投影当安装 manifest，不探测其它协议或重定向换源。v0.1 envelope 的 registry official metadata SHALL 只从顶层 `_meta` 读取，发布者的 `server._meta` 不得冒充目录身份；来源没有稳定 ID 时保留缺省，以名称、精确版本、来源及完整 manifest digest 冻结，不合成 ID。variant SHALL 同时绑定原 ordinal 与内容 digest，消费前由原 MCP owner 重读来源并核目标原生平台、参数及计划 digest；候选生成和重验本身不下载包、不启动 MCP、不写配置或安装账本。

#### Scenario: 安装一个无需包的 HTTP MCP
- **WHEN** 用户从目录选择 HTTP 服务并为 A1 配置 endpoint
- **THEN** 展示配置、授权和连接阶段，无虚假包下载；只有 A1 沿原配置与 MCP owner 接入，A2 不被启用

#### Scenario: 选定变体失效且仍有其它候选
- **WHEN** manifest 含多个 package／remote，用户所选 variant 在提交前消失，而首项为另一包或 SSE endpoint
- **THEN** 原计划失效并要求明确选择，不自动安装首项、不改传输类型，也不写入半份成功配置

### Requirement: 包安装状态必须由实际执行目标持有

实际安装服务 SHALL 唯一持有制品、精确版本、来源、校验摘要和操作状态，并提供可追踪的有界进度及错误。Gateway／前端只展示该状态及其新鲜度，MUST NOT 维护另一份可独立生效的安装账本。安装包准备、验证、发布、配置保存／待生效、连接就绪及工具目录发布 SHALL 分别表达；安装完成不等于 Agent 可调用。

目标侧实际制品安装是新增能力，不能以原版写 gallery manifest／动态 npx、uvx、docker、dnx 启动配置代替。受管 launch SHALL 绑定已验证的隔离制品 generation／digest 和锁定依赖，容器绑定不可变镜像 digest；不具备该安装方式时明确不支持。已有用户显式动态命令保持原合同，不追认为受管安装或自动改写。

PyPI wheel 能力 SHALL 使用目标已有 uv／Python、显式 index 和锁定依赖，不自动安装系统 runtime。标准 wheel、根 distribution 的实际版本及用户显式 console entry point 验证完成后才发布。venv 准备 SHALL 从最终固定 generation 路径开始，不能先准备再改名破坏绝对 shebang。

npm 能力 SHALL 使用目标已有 Node／npm、显式标准 registry、精确 semver 及 `package-lock` v3／`npm ci`。发布前验证根 package 的名称、版本及用户明确选择的 JavaScript bin，依赖来源、integrity 与实际依赖树沿同一生态工具核对。发布记录 SHALL 保留目标实际 Node 路径和版本；启动只使用该 Node 和原 generation 中的 bin，不通过当前 PATH、npx 或 latest 重新选择。PyPI 与 npm 的 capability 和不可用原因 SHALL 分别报告，窗口根据用户选定 variant 核对对应能力，不因另一生态缺失改投或阻止可用生态。首批 npm 不执行 install scripts，需要该构建步骤的实际依赖明确拒绝；OCI／NuGet、sdist、自定义 runtimeArguments 和目录单文件 fileSha256 未有验证链时明确拒绝，并在 capability 返回具体限制。

同一 BOXTEAM_HOME／稳定 Backend 安装域的多个进程 SHALL 共享唯一 SQLite 安装账本；操作 reserve 由事务竞争唯一取得，活进程持有的操作不因另一 Backend 启动被判中断。只有原 PID 与创建时间已确认不存在时才标记 interrupted，相同意图不重放；发布制品和操作成功 SHALL 在同一事务中提交。

#### Scenario: 同 home 第二 Backend 观察原安装
- **WHEN** 同一安装域的另一真实 Backend 启动并查询原安装或操作
- **THEN** 它观察唯一发布记录及活进程状态，不能阻止第二 Backend 装配或中断原操作；两个工作区的配置和 MCP 连接保持独立

#### Scenario: 包已安装而配置准备失败
- **WHEN** 包验证完成，但目标工作区配置因无效参数未能激活
- **THEN** 包状态如实保留已安装，配置显示真实失败与原原因，Agent 不得到伪造 ready 目录

#### Scenario: 真实启动使用已发布锁定制品
- **WHEN** 目标完成服务安装后，目录 latest 改变，用户从真实 Thread 启动并调用服务
- **THEN** 实际 launch 路径／镜像及依赖仍匹配原安装 generation／digest，未重新动态解析 latest；仅写配置或启动时临时下载不能算通过

#### Scenario: npm 制品发布后环境与 registry 更新
- **WHEN** npm 根包和依赖已锁定发布，registry 增加新版，目标 PATH 中的默认 Node 改变，用户显式配置原制品并经真实 Thread 调用
- **THEN** 启动使用发布记录中的 Node 路径和原 generation 的显式 bin，根包与依赖保持原版本；原 Node 缺失或不可执行时明确失败，不切换 runtime 或下载新版

#### Scenario: npm 入口无效或依赖需要构建
- **WHEN** 用户明确选择的 bin 不属于根包，或实际依赖需要尚未支持的 install scripts
- **THEN** 原操作明确失败，不发布可启动 binding，不改写旧制品或工作区配置，也不退回依赖包入口或动态 npx

### Requirement: 安装操作必须保持幂等并可核实中断结果

安装及更新 SHALL 具有绑定目标／制品的操作身份、幂等意图、状态查询和可复查日志。断流、超时及取消 MUST NOT 被解释为目标已停止或已回滚；重连 SHALL 查询原操作，不自动重放未知安装脚本。重启 SHALL 对账实际已发布状态并明确未确认阶段，不伪报成功或清空旧记录。

同一幂等键 SHALL 绑定规范化目标、来源／manifest、variant、制品及非秘密配置意图指纹；相同意图重试返回原操作，参数不匹配明确冲突，不复用键执行另一安装或在日志记录原始秘密。

用户取消 SHALL 针对独立 operation，终态使用 `cancelled`，不将取消伪报为失败或中断。多个 operation 共享制品准备或下载时，取消其中一个 SHALL 立即结清该 operation，其余操作继续；最后一个使用者取消后才停止实际任务，并核实执行进程退出。同一安装域 SHALL 支持跨后端进程取消：在现有 SQLite 安装账本提交取消意图，由原执行者处理并发布实际结果，不建立第二取消账本。取消与发布 SHALL 沿原事务结算；成功发布先提交时保留 `succeeded`，不能由迟到取消改写。

#### Scenario: 安装发布时客户端断开
- **WHEN** 客户端在制品发布阶段断开，重新连接后提交同一安装意图
- **THEN** 返回或查询原操作及实际结果，不再次执行安装脚本；无法确认的阶段保持明确中断待核实

#### Scenario: 共享下载中的一个操作取消
- **WHEN** A1 与 A2 的安装操作共享同一次下载，用户取消 A1
- **THEN** A1 独立进入 `cancelled`，A2 继续使用原下载；仅最后一个使用者取消才停止实际任务并核实退出

#### Scenario: 跨进程取消与成功发布竞争
- **WHEN** 同安装域的另一后端提交取消意图，而原执行者正在发布制品
- **THEN** 原执行者消费持久意图并沿原事务确定结果；成功发布已先提交则保持 `succeeded`，否则按实际取消结算，不重放安装脚本

### Requirement: 共享制品不得合并工作区连接与凭据

同一明确安装域 SHALL 可复用同一精确版本制品；不同 workspace 的配置、凭据、连接、目录及调用资格 MUST 继续独立。卸载某工作区使用配置 MUST NOT 删除其它工作区配置或共享制品。物理卸载 SHALL 检查并列出实际引用，有引用时明确拒绝；旧版本的运行引用及原调用资格未结算前不得删除。

制品 generation／digest SHALL 在原连接代际建立、进程准入前绑定，引用沿原 connection retirement／调用监督事实释放，不另建调用租约账本；子进程退出未验证、cleanup_failed 或未结算调用仍需制品时不得删除。

#### Scenario: 同后端 A1 与 A2 使用相同服务
- **WHEN** A1/A2 引用同一安装版本、以不同凭据连接同名 server，用户移除 A1 配置后尝试物理卸载
- **THEN** A2 连接与目录保持不变，卸载因 A2 或在途引用明确拒绝，不把两者合成同一逻辑 session

### Requirement: 新管理入口必须沿唯一工作区 MCP 生命周期

原 MCP 管理视图、工作区连接入口和项目 Agent SHALL 使用同一工作区配置、连接、OAuth、工具目录与调用监督事实。现有安全热替换、目录 revision／连接 generation 分离、封存资格、排空、结果未知与恢复合同 MUST 保留。VSIX 贡献服务定义时 SHALL 仍经明确目标和配置流程启用，不建立另一个项目 Agent MCP client。

配置提案 SHALL 由原 MCP owner 重新消费冻结候选与已发布制品，生成指定 Server 的 JSONC 编辑并经原来源 owner 无副作用预检；调用方不得按脱敏计划参数重复拼装 launch。提案只返回脱敏来源编辑和预检结果，不回显原候选输入或目标机器解析出的敏感值；源文档中其它成员、注释和格式保持。新候选的脱敏占位 SHALL 在保存时由同一候选重新验证并还原，其它来源占位仍由原文件 writer 还原，不建立提案存储或第二保存链。原生环境引用保持原表达式，不能将解析出的目标环境值固化为新配置。

#### Scenario: 生成提案后按原 CAS 保存
- **WHEN** 用户为明确工作区选择原候选、已发布制品与启用状态，提交来源文档及原 CAS 基线
- **THEN** 提案生成不写文件、不启动连接，响应中不出现秘密；确认后原保存入口重新核候选并消费同一来源基线，创建实际配置，同键重试返回原结果，基线变化明确冲突；更换 Server、launch 或秘密绑定不能沿用该占位

#### Scenario: 从原版管理视图启用并调用工具
- **WHEN** 用户通过原 MCP 管理入口为 A1 启用测试服务，并从 A1 的真实 Thread 调用其工具
- **THEN** 原工作区目录与实际调用使用同一连接身份，A2/B1 同名 server 不被调用；连接或授权失败不能由另一套前端目录掩盖

#### Scenario: 更新版本时仍有旧调用
- **WHEN** 新版本安装完成而旧连接仍有封存资格或结果未知的操作
- **THEN** 明确拒绝切换并保留旧配置、原连接与原 launch binding，既有调用继续由旧版处理；旧调用、封存资格及结果未知均结清后，用户才可再次显式切换至新版。cleanup_failed 或结果未结算期间保留必要引用并拒绝卸载，不自动重试切换、不改投或重放外部工具，不启动新旧 stdio 并行进程

### Requirement: VSIX 服务定义必须保持动态 Provider 生命周期

VSIX 定义 SHALL 绑定公开 extension ID、稳定目标／安装实例、host generation、provider／collection 及原定义身份／版本。provide 阶段不触发交互认证；启动前调用原对象的 resolve，保留取消、undefined 拒绝、异常、变更事件与 dispose 语义，结果仍交项目 MCP owner 校验使用。短期秘密不得固化为普通配置。来源变化、卸载、host 不可用或旧 generation 迟到须使未启动候选失效，已有配置保留可诊断的来源引用及 unavailable 状态，不静默删用户配置或启用缓存 launch。已准入调用沿原监督处理；只有明确导出的独立静态配置才可脱离 Provider 恢复。

动态定义的来源执行上下文 SHALL 与运行目标匹配；Node Provider 的 stdio／本机路径／env 不得直接用于另一目标，Web Provider 不得把浏览器路径当目标原生路径。跨目标只允许明确导出的可迁移静态模板重新经过目标校验、配置和凭据选择；HTTP endpoint 也不得隐式搬运来源 token／localhost／授权上下文。没有明确合同须拒绝。

#### Scenario: 同 ID 提供者跨目标更新并离线
- **WHEN** A/B 同 ID 扩展提供不同定义，A 的 Provider 发变更后 dispose／离线，旧 resolve 结果迟到
- **THEN** A 未启动候选失效且不能借 B 或旧缓存启动，原配置保留可诊断状态；B 定义不受影响，旧代际结果不发布，已准入调用按原 MCP owner 结算

#### Scenario: A 的动态定义被选到 B 运行
- **WHEN** A 的 Provider 提供本机 stdio 定义，用户选择 B workspace 启用，且没有明确可迁移静态模板
- **THEN** 启动前拒绝来源与执行目标不匹配，不在 B 使用 A 的路径、env 或秘密；可迁移模板须另经 B 的正式候选链，不冒充原动态定义

### Requirement: MCP 安装与配置写入必须由原生目标授权

目标发现／读取权限 MUST NOT 自动授予安装、进程执行或配置写入。远端原生 owner SHALL 最终校验对应管理能力，Relay 不提升权限、不直接修改远端业务文件。秘密 SHALL 留在实际消费端的私有存储，普通配置投影、日志、浏览器缓存与历史不得包含凭据值。

候选 manifest 读取、确切 variant 规划、消费前重验和制品安装管理 SHALL 核原 policy 的 `mcp_installation_manage`，默认拒绝，仅精确绑定已认证主体的显式 allow 授权。普通 read、discovery、managed_workspace_admin 和配置读写均不代授；配置保存继续独立核 configuration_write，重启与宿主管理沿各自能力。无安装管理权限时，原生 Gateway SHALL 在访问 Backend 或目录来源前拒绝；Relay 不改用本机权限。

现有 OAuth token store 不代表已具备通用 input secret 能力。系统 SHALL 在原 MCP 凭据／配置 owner 补齐工作区私有输入写入、引用 schema／API、连接端解析及版本合同，身份区分稳定 workspace、server、输入键／版本及服务来源／endpoint。值保存在该工作区 `.boxteam/credentials/`，不放目标共享制品区；未实现该能力前明确拒绝依赖它的新条目。旧明文输入和 OAuth 保持原合同，不能自动迁移或用遮罩覆盖原值。

单个 Streamable HTTP 私有 header 的写入口 SHALL 只返回绑定 workspace、server、header 名、精确 endpoint 及不可变版本的引用；原 source／candidate／CAS 保存该引用，原 SDK session 准备时在实际消费工作区解引用。私有值 MUST 是无控制字符、无首尾空格的非空 ASCII 文本，非法值在写入前拒绝，不静默修剪，也不交给 transport 生成含原值的异常。引用不得随 HTTP 重定向送至另一地址。经联邦管理入口写入 SHALL 同时满足原 configuration_read／configuration_write，且在等待和响应披露边界重验；该能力不表示已支持任意目录 input、env 私有引用或完整安装 UI。

#### Scenario: 只有发现权限的远端连接
- **WHEN** 用户可查看 B 目录但没有 B 的安装或配置管理能力
- **THEN** 实际管理请求被 B 明确拒绝，B 没有下载、配置提交或进程执行，本地也不改投替代目标

#### Scenario: 同名服务的私有输入重启恢复
- **WHEN** A1/A2 为同名服务设置不同 secret header／env 并重启，再从各自 Thread 调用，B 同时查看允许的配置诊断
- **THEN** 消费端解析各自有效引用且工具实际使用正确值；B、普通来源正文／DTO、事件、日志及浏览器存储均不取得秘密，换 endpoint 不自动借用旧秘密

#### Scenario: 私有 header 的非法空白在写入前拒绝
- **WHEN** 用户写入带首空格、尾空格或控制字符的私有 header
- **THEN** 原写入口明确拒绝，私有文件和配置没有新增版本，响应与普通日志均不包含原值，不因自动修剪改变用户输入

### Requirement: MCP 管理必须复用原入口并暴露真实操作

管理界面 SHALL 使用原列表／详情／命令及目标分组，明确显示安装目标、启用工作区、来源、版本、运行与授权状态；安装、配置、连接、断开和卸载的语义必须可辨认。适用操作 SHALL 接入原命令及快速访问机制；旧入口与新入口消费同一事实，不能维护第二套 enabled／status。

#### Scenario: 在两个入口查看同一连接
- **WHEN** 用户在扩展视图与 A1 底部 MCP 连接入口查看同一 server 并断开
- **THEN** 两处展示同一 owner 返回的状态和操作结果，底部仍按 A1 保存状态，右侧会话资源不承载全局安装管理
