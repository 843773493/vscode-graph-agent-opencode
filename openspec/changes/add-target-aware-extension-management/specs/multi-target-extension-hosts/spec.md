## Purpose

定义项目自有插件的声明式贡献、运行期 Provider 与受管服务三层扩展架构，复用现有工作台能力并统一目标、注册及释放边界；首批开放少量扩展点，浏览器、终端、源码调试与 MCP 共用 Gateway 托管，工作区业务事实保持原归属。此能力目录沿用规划索引，不提供 VS Code Extension Host 兼容。

## ADDED Requirements

### Requirement: 插件包必须采用多插件自有格式并退出旧扩展执行体系

插件包 Plugin Package SHALL 是标准 .zip 包或同结构显式开发目录，根 plugin.jsonc 声明 manifest_version、package_id、version、名称说明、extension_contract_version 和 plugins。每个插件 Plugin SHALL 声明包内唯一 id（外部绑定的 plugin_id，与 package_id 共同确定逻辑插件）、名称、固定 gateway／workspace instance_scope、requires 与 contributes；有运行代码时才声明 Python／Node runtime、相对包根入口及参数数组和 management_protocol_version。贡献 Contribution 全部归 plugins[].contributes；运行实例 Runtime Instance 属于精确插件绑定。系统 MUST NOT 保留根级 runtime／entrypoint／scope／contributes、components／component_id、modules／features 双套模型或开发格式兼容。名称不能代替稳定 ID，入口不能为 shell 字符串。

包是整体安装／版本／卸载单位，插件是独立开启／关闭／重启单位；同包运行插件分别使用独立实例，插件内工具和 Skill 共用该插件生命周期，纯声明插件无运行入口。系统 SHALL 删除项目接入的 VSIX、市场、用户 Extension Host／vscode API／RPC 和旧动态 Provider，保留原 Workbench、内置贡献及业务实现。

#### Scenario: 一个包内两个插件独立运行
- **WHEN** 用户安装含终端与浏览器插件的合法包，只开启终端，再开启／重启浏览器
- **THEN** 包只有一份安装版本，两个代码插件使用独立入口及实例；浏览器动作不改变终端实例／输出，关闭浏览器不撤销终端贡献

#### Scenario: 声明插件与旧格式拒绝
- **WHEN** 用户启用没有运行字段的合法 Skill／菜单插件，或提交根级单入口旧设计、components／component_id 或 modules／features 格式、改名 VSIX 或未知合同
- **THEN** 声明插件无需进程可用；其他输入在发布前明确拒绝，不转换或启动旧宿主，不部分安装非法包

### Requirement: 插件包必须按显式清单解析并分离安装内容与运行数据

根 plugin.jsonc SHALL 是插件包的唯一清单，各插件入口和声明资源路径按包根显式解析；Skill 正文相对引用沿原 Skill 目录合同。内部目录名称及入口文件名 SHALL 不参与插件身份或发现协议，纯声明插件不要求运行入口。自带包、下载包和显式开发目录 SHALL 使用同一包合同，缺文件或路径越界在原发布／准入边界明确拒绝，不递归扫描猜测插件或生成每插件第二清单。

归档条目及解析后的入口／贡献资源 SHALL 保持在包根物理边界内；绝对路径、越出包根的路径穿越和指向包根外的链接必须在写出或发布前拒绝，不以字符串前缀相同判定包含关系。开发目录在显式重载时使用相同边界，不增加普通调用扫描。

已发布包内容 SHALL 保持只读，由原生目标唯一安装 owner 管理；运行日志、缓存、参数、秘密和业务产物不得写回包目录。实例控制数据及诊断日志 SHALL 沿原 Gateway owner，工作区业务数据仍归原工作区 owner；Skill 通过原资源链读取包内只读资源，不复制到各工作区或客户端形成安装副本。目录校验 SHALL 在安装发布及显式重载等原边界执行，不成为普通请求的递归检查或全包 hash。

运行工作目录和可写输出 SHALL 使用对应原 owner 的合法目录，包作为不可变输入；此约定不宣称 Python／Node 进程受到安全沙箱强制隔离，也不要求新增沙箱体系。

#### Scenario: 合法调整包内目录布局
- **WHEN** 作者调整插件入口和 Skill 目录位置并同步根清单，再安装该包
- **THEN** 系统从声明路径提供同一逻辑插件及资源，不依赖约定子目录或文件名，不扫描出另一插件；纯声明插件可用且零进程

#### Scenario: 运行不改写已发布包
- **WHEN** 用户安装插件包，启用插件、加载随包 Skill、执行工具并重启实例
- **THEN** 安装内容保持，控制数据、诊断日志和工作区业务产物分别进入原 owner 的合法目录；Skill 读取没有创建工作区副本，也没有逐请求全包检查

#### Scenario: 归档路径或链接逃逸被拒绝
- **WHEN** 包含绝对路径／越界归档条目，或声明入口／资源经链接解析指向包根外
- **THEN** 在写出越界文件或发布前明确拒绝，不产生包根外安装文件，不将非法候选替换已发布包；合法包内相对路径仍可使用

### Requirement: 扩展点目录不得转移原业务服务的权威归属

产品 SHALL 维护唯一扩展点支持目录，集中声明标识、合同版本、支持状态和原服务接缝；具体贡献的校验、注册、释放及调用 SHALL 由对应原业务／工作台服务处理。安装、注册和客户端 SHALL 消费同一合同及生成协议，不各自维护另一支持集合、命令表、技能目录或有效配置。Gateway 插件管理 SHALL 复用原进程与配置机制，不因为新增管理目录建立第二 supervisor 或接管 Agent 业务。

#### Scenario: 一个插件同时贡献命令和 Skill
- **WHEN** 用户开启同时声明命令与 Skill 的合法插件，再从命令面板和原技能入口使用
- **THEN** 两类贡献分别进入原命令及 Skill owner，绑定同一插件身份和版本；关闭后各 owner 按原规则释放，新管理目录不产生第二命令／技能事实源

### Requirement: Backend 与客户端必须经 Gateway 访问精确服务实例

Backend SHALL 只通过 Gateway 的具名 HTTP／流式服务路由消费插件，不直接创建插件、MCP stdio、Node 调试目标或安装子进程，也不保存插件监听端口。python_exec、持续任务条件脚本、视频预处理 ffmpeg SHALL 保留现有 Backend 调用与执行，不受上述插件执行迁移要求影响。Terminal／Browser 交互与 Debug 通道 SHALL 同样经 Gateway，Debug 业务准入仍经过 Backend 原 owner。目标由稳定 Gateway、package_id／plugin_id、service_id、合法 workspace 和运行 instance_id 解析；未知、离线或旧运行身份 MUST 明确失败，不选择同名新实例、活动工作区、本机端口或任意请求 URL。

Gateway SHALL 仅处理授权、路由及托管，不解释 Agent 工具策略、不保存工作区业务状态；原服务继续管理 PTY／浏览器／stdio 子进程和领域状态，原 Backend owner 处理会话关联与结果。

#### Scenario: 三种服务均经真实 Gateway
- **WHEN** Backend 调用 MCP、创建终端或操作浏览器，客户端随后附加终端／浏览器流
- **THEN** 所有连接均经过目标 Gateway 到同一服务实例，保留资源身份、顺序、流控和原错误；这三条执行链没有 Backend 子进程或服务端口旁路

### Requirement: 插件绑定必须支持 Gateway 和工作区两种明确范围

插件 SHALL 固定声明 gateway 或 workspace instance_scope。gateway 插件在原生 Gateway 绑定下单实例，workspace 插件按精确工作区独立运行；package_id／plugin_id／原生 Gateway／必要 workspace_id 组合 SHALL 只有一个生命周期槽位，确切包版本是该槽位属性及注册调用核验值，不得按版本并行创建第二槽位。每绑定持有确切包版本，service_id 仅为插件内协议路由，不另有启用开关。共享包或 Gateway 实例不共享业务权限／凭据。纯声明插件使用相同范围和开关但无进程实例。

已启用 Gateway 插件 SHALL 随 Gateway 生命周期恢复，已启用 workspace 插件随所属工作区运行时恢复／完整停止；新启动所需的已提交非秘密绑定 SHALL 由 Workspace 配置 owner 重新认证交付，尚未交付则等待，不读 .boxteam 或从旧进程／库存推断 enabled。Backend 单独重启或 Gateway 正常换代时，已核实原实例沿原接管合同保留，不因暂待交接而停止、改绑或重复启动；Backend 单独重启、页面关闭、Tab／导航切换不得改变 enabled 或停止插件。普通工具、命令、Skill 访问及管理读取 MUST NOT 启动任何插件；新建工作区默认值不是第三种运行范围。

#### Scenario: 工作区配置 owner 尚未交付恢复绑定
- **WHEN** Gateway 完整重启，而目标 Workspace owner 尚未就绪，或只是 Backend 单独重启
- **THEN** 前者等待认证配置交接后才新启动，不读工作区目录或猜测开关；后者保留已有核实实例，不因暂时断连新建／关闭插件

#### Scenario: 同包跨范围和跨工作区隔离
- **WHEN** 同包有一个 gateway 插件和一个 workspace 插件，A1／A2 均启用后者
- **THEN** Gateway 插件一个实例，workspace 插件两个实例；A1 关闭只停止 A1 对应实例和贡献，不能关闭 Gateway／A2，Backend 单独重启不替换实例

### Requirement: 所有服务必须实现统一且有版本的管理合同

有运行代码的插件 SHALL 提供匹配实例身份的 ready、status、shutdown、诊断配置 apply／status 和所声明统计能力；ready／status 管理回执 SHALL 以插件实例为身份，报告 package_id、包版本、plugin_id、scope、instance_id、管理及扩展合同版本、能力和可为空的服务清单；仅有命令代码时不要求服务，service_id 只用于清单和具体业务路由，不作为插件管理单值身份。纯声明插件 SHALL 使用独立贡献状态，不伪造运行中进程或 ready。启动、接管、配置应用和停止 SHALL 复用现有进程 owner 与跨平台原语，不建立第二进程管理器、通用远程 shell 或插件业务数据库。

进程状态 SHALL 为 stopped／starting／ready／stopping／failed，连接状态独立报告；进程已创建不等于 ready，断连不等于进程死亡。完整停止 SHALL 在一个总期限内停止新准入、处理在途工作、关闭实际子进程并核实退出；失败保留具体阶段和原错误，不伪报已停止。

#### Scenario: Python 与 Node 就绪及退出行为一致
- **WHEN** 两种实现返回错误协议／实例回执，或停止 ACK 后子进程仍存活
- **THEN** 前者不得发布 ready，后者不得报告 stopped；相同合同测试适用于两种运行时，错误沿原进程诊断可见

### Requirement: 默认断连与重启不得恢复未知副作用

连接断开 SHALL 结束或明确失败本次等待，保留已知结果或结果未知，不自动重放写操作。插件进程退出 SHALL 报 failed 和实际退出信息，默认不循环重启；显式重启 SHALL 在旧进程退出核实后创建新 instance_id，旧句柄失效。请求取消只作用于原操作，不停止共享服务或宣称远端回滚。

Backend 重连可以重新读取原实例状态；服务存活不证明旧 execution 已恢复，服务重启不保证原 PTY／浏览器／MCP 会话仍可用。已有合法资源查询／attach 能力 SHALL 保持，资源丢失明确失败，不以新资源冒充。专用恢复策略 SHALL 后续按服务需求显式扩展，不预建通用持久参与者账本。

#### Scenario: 写操作响应丢失与显式重启
- **WHEN** 写请求已到达插件但响应丢失，之后用户重启服务
- **THEN** 原操作保持实际结果或未知且零自动重发；新实例使用新身份，原资源／调用句柄不能静默迁投

#### Scenario: Backend 重启后重新附加原资源
- **WHEN** Backend 停止并重启，而终端／浏览器服务及其资源继续存活
- **THEN** 经 Gateway 查询原资源并按原合同 attach，不创建替代资源或重放原命令；资源已失效时明确报告

### Requirement: Gateway 必须热应用可关闭的本地诊断与统计

日志 SHALL 复用原 Gateway 配置与运行期热应用合同；新增遥测／统计 SHALL 使用同一配置 owner 和受控 revision，默认关闭。关闭对应能力 SHALL 停止额外采集、采样定时任务和缓冲维护，不隐藏必要错误。新实例取得当前配置，存活实例回执实际 instance_id／revision／设置；部分失败逐项报告，不能把保存或 HTTP ACK 当作全实例已应用。

启用统计时 Gateway SHALL 统计实际代理／进程指标，插件内部指标须由声明支持的服务提供；未支持指标明确说明，秘密和业务正文不进入通用遥测。第一版只本地诊断，不向公网目录上报。普通请求 MUST NOT 附带全包 hash、进程扫描、逐请求健康探测或完整历史审计，遵守现有完整交互总延迟预算。

#### Scenario: 运行中关闭遥测及部分应用失败
- **WHEN** 已启用采集的 Python／Node 服务收到关闭 revision，其中一个明确应用失败
- **THEN** 成功实例停止额外采集，失败实例显示原实际状态与错误，整体不伪报成功；普通请求仍可按真实可用性执行，不重启服务强迫生效

### Requirement: 插件管理 UI 必须按插件包分组并在明确范围管理插件

UI SHALL 复用原管理列表／详情及目标选择器，包级提供安装／整体更新／卸载，包内按插件展示固定范围、启用开关和可用重启，各运行实例分别提供状态、日志和错误；组标题的全部启用／停用只是批量操作。仅有“当前 Gateway”“当前工作区”“新建工作区默认值”三类管理对象；最后一项只编辑创建模板，不操作当前实例。用户不能在 workspace 面板误关共享 Gateway 插件。纯声明插件显示启用状态而不伪造进程 ready。

包级全部开启／关闭 SHALL 只纳入当前管理目标中固定 scope 匹配的插件，执行前列出纳入及排除项：Gateway 视图排除 workspace 插件，工作区及新建默认值视图排除 gateway 插件，模板批量只写模板；不跨范围隐式追加。范围内权限不足或执行失败仍逐项报告，不伪装成范围排除。操作 SHALL 返回逐项结果，不新增包级 enabled、跨插件事务或自动依赖启用。开关经后端配置 owner 提交，UI 不以本地状态冒充成功。列表目标变化影响后续新动作；详情、草稿、在途开关冻结原目标，焦点和迟到响应不改投。

插件绑定及运行实例的 API／UI 结果项 SHALL 显式携带原生 Gateway、scope、必要 workspace_id、package_id、plugin_id、确切包版本及适用的 instance_id；纯声明插件不伪造实例字段。安装库存仍按包报告，不因尚无绑定而要求 workspace 或实例。列表可按包分组，但不得把不同目标／版本的绑定合并成同一状态行。

#### Scenario: 混合范围插件包的批量操作
- **WHEN** 同一包包含 Gateway 与 workspace 插件，用户在当前工作区点击全部停用
- **THEN** 仅关闭该工作区的 workspace 插件，执行前列出 Gateway 插件因范围排除；Gateway、另一工作区及新建默认值均不被修改，范围内失败单独报告

#### Scenario: 包内批量关闭部分失败
- **WHEN** 用户关闭当前工作区包内插件，其中一个成功停止，另一个停止失败，随后切换列表到另一工作区
- **THEN** 显示原工作区逐插件实际结果，失败者不伪报关闭，另一工作区不受影响，不创建包级混淆开关或回滚已完成操作

### Requirement: 安装运行与数据删除必须分别执行

安装 SHALL 由目标唯一安装 owner 校验、准备锁定依赖后发布，不自动运行服务或改工作区配置；需要进程的准备步骤 SHALL 经统一受管边界执行。更新发布新不可变版本，原运行实例继续绑定原版本；卸载 SHALL 在原实际运行／读取／使用引用结清后删除对应版本，有引用时明确拒绝并显示引用，不隐式关闭资源。

卸载引用 SHALL 只从原 owner 的四类事实读取：实际运行或尚未核实退出的实例、原配置 active／pending 绑定（关闭但未解除也计入）、Gateway 新建模板及未结清创建 intent、原资源 owner 未完成的读取／使用 lease。可释放 UI 投影先结清，不形成永久引用；完整封存正文与未绑定参数残留不占包引用。原安装 owner SHALL 经现有登记定位引用 owner，并复用原拓扑／删除互斥边界，避免引用建立与删除竞争；不能确认引用时拒绝本次卸载并指出未知来源，不把未知当空集、不为检查启动 Backend、不扫描目录或建立全局引用账本／巡检器。

v1 MUST NOT 执行任意安装／卸载 hook 或 vscode:uninstall，不以退出／更新自动删除配置、秘密或业务数据。旧实现移除前 SHALL 全仓迁齐直接与间接调用、生成协议、构建和测试；历史验证不得证明新格式已实现。

#### Scenario: 更新与有引用卸载
- **WHEN** v1 正在运行时安装 v2 并尝试卸载 v1
- **THEN** 原实例继续使用 v1，卸载返回实际引用冲突，v2 不接管旧句柄；引用结清后的删除只作用于该安装版本，不执行旧脚本或清空工作区数据

#### Scenario: 引用 owner 不可确认
- **WHEN** 用户卸载某版本，而原登记中的工作区引用 owner 无法确认其 active／pending 绑定
- **THEN** 卸载明确拒绝并列出该来源，包保持；零 Backend 自动启动、扫盘和后台修复，不将已停止工作区视作没有绑定

### Requirement: 打包客户端必须复用目标侧服务链

Web 与 Electron SHALL 构建同一工作台；插件运行在目标 Gateway 所在环境，Electron 只承担既有窗口、生命周期和固定存储。发行物 SHALL 提供所需管理组件、自带插件和静态资源，启动及自有离线包安装不得依赖源码、开发服务器或 Electron Node API。界面仅在相应合同可用时声明可运行。

#### Scenario: 源码目录外启动打包产物
- **WHEN** 关闭开发服务器后从源码目录外启动 Electron，连接已有 Gateway 并使用终端／浏览器／MCP
- **THEN** 三者经同一目标 Gateway 服务链完成真实操作，重启保留原合法配置和资源关联，不启动旧用户 Extension Host 或新增原生 IPC

### Requirement: 扩展点目录必须区分完整架构与实际开放能力

系统 SHALL 统一描述声明式贡献、运行期 Provider 和受管服务，公布每种扩展点的合同版本、运行属性与实际支持状态。首批只开放 commands、menus、keybindings、configuration、services、debuggers、skills 七类贡献；debuggers 按本规范限定自有声明和 DAP，不开放 vscode.debug API，skills 接原 Skill 资源及上下文 owner。Quick Access 查询、树／视图、编辑器、文件系统／文本内容、搜索、大纲／符号、语言／Diagnostics、通用 Terminal／Task Provider、Notebook、Webview、主题等其他贡献 SHALL 标记未开放，不因上游存在代码或项目内置实现而宣称用户插件可注册。SCM、状态栏、认证、测试和 Chat 扩展同样适用。

安装／启用 SHALL 校验全部所需扩展点和实际声明；未知或未开放种类、不兼容版本明确拒绝且没有部分生效，不静默忽略声明。系统 SHALL 维护基础机制、项目业务接入、自有插件开放与真实验收的独立状态；扩展目录不构成空 Provider、通用 RPC 或完整 SDK 的实现要求。

#### Scenario: 上游有树视图仍拒绝未开放贡献
- **WHEN** 包声明树 Provider 或目标 Notebook 能力，而当前只开放首批集合
- **THEN** 安装／启用前返回具体未开放扩展点，不注册空视图、不启动旧宿主、不把包报告为完整可用；原项目会话树和编辑器保持

#### Scenario: 已声明和已协商能力必须一致
- **WHEN** 运行实例注册 manifest 未声明的处理者，或要求不兼容的扩展合同版本
- **THEN** 该注册在可调用前明确拒绝，不能因管理协议 ready 或同名命令存在而放行

### Requirement: 首批贡献必须复用工作台原交互与配置规则

已启用插件 SHALL 通过自有声明向原工作台／业务服务贡献命令、菜单、快捷键、配置、服务、调试器和 Skill；命令标题／分类、参数与结果合同、处理者及所需业务能力必须明确。菜单只使用公布的位置、分组及命令，快捷键沿原平台键位与用户覆盖规则；条件表达式只使用公布的上下文键和原解析规则，不执行包代码或任意表达式。产品内部命令只有明确导出的集合可被声明引用，不能转发任意内部 command_id。调试器声明 SHALL 包含自有类型、配置 schema、关联 debug 服务及 DAP 版本／能力，不携带 renderer 脚本或 VSIX adapter 依赖；Skill 不要求新增 Workbench 专用注册器或运行进程。

命令 SHALL 出现在原命令面板并使用原可用性、快捷键与菜单交互，不建立另一命令表或 UI。声明可见性不授予业务访问。配置 schema SHALL 在插件停止时仍可合法读取，保存交原配置 owner；展示声明、计算条件或查询设置不得启动 runtime。

命令可选 enablement SHALL 作为原 UI precondition，菜单项 when 控制该位置的呈现，插件快捷键有效条件 SHALL 合取命令 precondition 与自身 when，并保留原用户键位覆盖规则。程序化调用 SHALL 独立核实际业务权限、目标及运行可用性，不将 enablement 或菜单呈现视为授权。已填写表达式 SHALL 通过原词法／语法诊断及产品公布上下文键检查；无条件只表示条件缺省，不得把非法条件解析结果或未知键降为无条件注册。

平台键位 SHALL 在消费端 Workbench 按客户端 OS／键盘映射和原用户覆盖解释，安装目标 OS 只决定插件运行兼容性，不得在目标安装／发布时裁去其他客户端平台的键位声明。

#### Scenario: 客户端与插件目标使用不同操作系统
- **WHEN** Windows 客户端连接 Linux Gateway 并使用同时声明 win／linux 键位的合法插件
- **THEN** Workbench 解析及提示使用 Windows 键位并遵守用户覆盖，业务执行仍到原 Linux 目标；目标发布未裁去 Windows 声明，也不因客户端 OS 改投运行目标

#### Scenario: 菜单呈现与命令启用条件分离
- **WHEN** 菜单 when 为真而命令 enablement 为假，插件快捷键自身 when 也为真
- **THEN** 原菜单按位置呈现但命令不可用，该插件快捷键不触发；程序化入口仍按业务权限和运行状态单独判定，不因菜单存在获得授权

#### Scenario: 非法条件不得变成无条件
- **WHEN** 候选贡献包含非法表达式或未公布的上下文键
- **THEN** 在原候选发布边界明确拒绝并指出贡献及字段，不注册无条件菜单／快捷键，不污染原已发布贡献或触发进程启动

#### Scenario: 同一命令在菜单和快捷键调用
- **WHEN** 用户从命令面板、声明菜单和快捷键触发同一插件命令，并已有用户快捷键覆盖
- **THEN** 三入口进入同一合法处理者，用户覆盖沿原规则生效；不重复执行、不绕过参数和权限验证，浏览标题不创建进程

#### Scenario: 非法贡献不得污染已存在命令
- **WHEN** 包试图覆盖产品内置 ID、引用未导出内部命令、使用非法菜单位置或条件键
- **THEN** 发布贡献前明确拒绝，原命令与快捷键不变，本次已新增的局部注册被释放，不回滚既有业务操作

### Requirement: 注册与调用必须绑定目标和运行代际

逻辑贡献 SHALL 使用 package_id／plugin_id 命名空间下的稳定 ID，注册归属包含明确 Gateway、必要工作区、包版本和启用绑定；运行期注册另含 instance_id、registration_id 与通道代际。同绑定重复 ID、跨插件冒名必须拒绝；同插件在 A/B 或多个工作区的合法绑定不得互相覆盖。

标题／分类、命令 precondition、菜单 when 及默认键位 SHALL 沿发起编辑器／视图的 scoped context 和合法目标版本投影，并包含产品约束的范围及实际可用性条件；命令面板绑定打开时的业务上下文，无目标时沿原选择流程。原 MenuRegistry 的隐式面板条目不得绕过这些条件；同 ID 全局元数据不可由不同目标逐次覆盖，键位不可取最后注册目标的 precondition。动作接受前仍须核原绑定有效，不因焦点或版本变化改投，不建立第二命令目录。

#### Scenario: 同命令跨目标的界面与执行同时隔离
- **WHEN** A v1／B v2 的同逻辑命令具有不同标题、启用条件和默认键位，用户分别使用两者所属编辑器并关闭 A 绑定
- **THEN** 标题、菜单呈现、命令面板和键位条件分别反映原目标，实际执行也到同一绑定；零隐式跨目标面板项，关闭 A 不删 B 的投影或唯一 dispatcher，B 不采用 A 的旧标题及条件

同逻辑 ID 的包版本或声明内容变化 SHALL 沿原 owner 候选校验和发布边界替换旧注册，不得仅凭 ID 未变跳过更新。替换 SHALL 先完整校验并构建候选声明，失败不动原发布；实际登记时先释放本绑定旧句柄，再按固定引用顺序注册，意外失败只释放本次新增句柄、报告贡献发布失败并停止后续发布，不重建已释放旧句柄。跨 owner 保留并报告已经发生的真实结果，不增加注册事务、回滚栈或补偿框架。仍残留的旧版本贡献只供诊断，不冒充新版本可用。

版本切换 SHALL 依次完成原 owner 候选预校验、原 writer CAS 提交期望并撤新业务准入、有代码插件停旧核退出再启新、按固定引用顺序发布贡献；纯声明跳过进程步骤。首次失败停止后续推进，停止失败不双起，新启动失败不自动恢复旧版；仅目标、绑定版本、贡献发布与适用运行状态一致才开放新使用。期望已提交时不自动回滚配置；用户通过代码插件显式重启、纯声明插件关闭再开启重新应用，选择旧版本是另一次显式配置操作，不新增版本组合账本或自动重试器。

客户端 SHALL 消费含目标、确切版本及原注册代际的完整快照，相同身份不重复装配，变化时只替换该绑定原 Disposable 句柄。标题、条件、键位和 schema 从快照派生，沿原 Menu／ContextKey／Keybinding／Configuration 服务解释；不分别保存可变镜像、不建元数据代理体系或逐字段同步协议，不在普通交互前远端发现。移除一个绑定只释放该范围句柄，其余合法绑定及仍被引用的唯一命令 dispatcher 保持。

每个目标的首次装配 SHALL 向对应原 owner 提供完整当前快照，不能只依赖连接后的增量。首批按固定最小引用顺序先发布命令，再发布引用它们的菜单／快捷键；菜单位置沿已公布的产品位置，不能借顺序规则开放插件自建 submenu。配置 schema 在对应目标设置发现前由原配置 owner 发布，运行能力仍须真实 ready 和注册完成后可用。系统 MUST NOT 为此引入可配置依赖图或通用拓扑调度器。

#### Scenario: 首次连接在贡献已发布之后
- **WHEN** 插件已发布声明后新窗口才连接或原消费处理者才安装
- **THEN** 原 owner 从完整快照恢复该目标合法贡献，命令先于引用它的菜单／快捷键可见；不丢失连接前声明、不重复注册，也不把未就绪处理者标为可执行

#### Scenario: 同 ID 声明更新与另一目标保留
- **WHEN** A／B 使用同逻辑插件，A 切换包版本并改变同 ID 命令标题／菜单／快捷键，随后收到重复快照并解除 A 绑定
- **THEN** A 在本 owner 成功发布后反映新声明而零重复项，解除只移除 A 注册；B 仍按原版本可用，不全局清空 dispatcher，失败候选不伪报更新

#### Scenario: 预校验失败与实际注册失败分别收口
- **WHEN** A 的候选声明预校验失败，或已通过预校验、释放 A 旧句柄后登记菜单时意外失败
- **THEN** 前者保留 A 原发布；后者释放本次新增句柄并停止后续发布、报告 A 不可用，不重建已释放的旧句柄；B 的合法注册和已确认业务结果保持

#### Scenario: 停旧后新版本启动失败
- **WHEN** 同槽位切换到 v2，期望已提交且 v1 退出已核实，但 v2 启动失败
- **THEN** 保留期望 v2 和真实 failed，不自动启动 v1 或重试 v2；用户显式重启才重新应用，普通列表与工具调用均零启动

发起调用 SHALL 冻结编辑器／视图提供的目标、原 Session／Thread 等业务身份、command_id、参数、调用身份和剩余期限；无明确接收目标时先选择，不用最后注册者或稍后焦点决定。输入／输出、进度、错误、取消按具名 typed 合同处理，Gateway 只授权／路由到精确实例，业务 owner 继续判权。运行管理、安装权限和菜单 when 不代授业务访问；插件命令也不自动进入 Agent 工具目录。

#### Scenario: 多目标同命令与切换焦点
- **WHEN** A/B 安装相同插件和命令，用户从 B1 Thread 发起调用，随后切焦点到 A1
- **THEN** 调用及结果始终属于 B1 的原绑定，包含原业务身份和 request_id；A1 不收到调用，B 离线或实例失效不转投 A

#### Scenario: 旧实例回执不能污染新注册
- **WHEN** 实例重启或原注册已释放，旧调用返回进度／结果
- **THEN** 新注册和新调用不受该回执影响；原调用保留真实结果或结果未知，不按同名命令重发

### Requirement: 插件启停必须只由开关和所属生命周期驱动

enabled SHALL 是原配置 owner 持有的插件期望状态，实际 stopped／starting／ready／stopping／failed 单独报告。开启保存 true 并在所属范围运行时可用后启动；关闭保存 false 后停止新工具／命令／Skill 使用准入，在一个总期限内取消在途、自有资源关闭并核实进程退出。重启仅作用于已开启有代码插件，先核退出再启动新身份，保持 enabled。范围尚未运行显示等待，启动失败保留 enabled=true 和原错误。崩溃不循环拉起；显式 UI 重启或所属完整生命周期恢复才可再启动。

插件绑定的开关／重启 SHALL 由原 owner 依配置 revision 串行处理；启动中关闭、停止中开启不得双起，旧 ready／回执不能复活已停用实例。纯声明插件开启只发布资源、关闭撤销新准入。工具／命令／Skill MUST NOT 隐式开启或启动插件，系统 MUST NOT 提供首次调用、automatic／on_demand／manual 多策略或要求普通调用者取得启停管理权限；未就绪直接返回实际状态。

#### Scenario: 启动中关闭与旧就绪回执
- **WHEN** 开启提交后启动未完成，用户关闭；旧进程随后返回 ready，并有工具调用到达
- **THEN** 原 owner 按最新期望停止该绑定自有进程，旧 ready 不发布工具，调用返回关闭／停止中状态且不重新启动，兄弟插件不受影响

#### Scenario: 已关闭插件收到命令或 Skill 加载
- **WHEN** 仅有业务权限的主体调用已关闭插件命令／工具或尝试新加载其 Skill
- **THEN** 返回实际不可用原因，零启动／重启／新注册；开启后合法业务调用不另要求该主体拥有进程管理权

#### Scenario: 停止失败与重启不得重叠
- **WHEN** 关闭时原目标未能退出，随后用户请求开启或重启
- **THEN** 显示原存活／失败事实并阻止新实例与旧进程重叠，核实退出后才启动一次，不按每层重新给予完整等待预算

### Requirement: 贡献释放必须覆盖停用与连接失效

每项静态／运行注册及订阅 SHALL 有明确 owner 与释放句柄。停用、更新绑定、工作区移除、窗口关闭、卸载和鉴权失效 SHALL 按对应范围及贡献类别撤销投影、监听与引用，不留可调用死入口；窗口关闭不停止其他消费者使用的 runtime。停用撤销命令／菜单／快捷键及工具／Skill 新使用，仍保留已安装版本的合法配置 schema／诊断读取；进程退出或通道断开只失效对应运行注册，不删除已安装声明。整包卸载释放对应 schema 投影，不依赖已删除包继续提供 schema，其他目标及仍合法使用的版本不受影响；不可用处理者不得伪报可执行。

重连 SHALL 仅读取原快照并在合法代际重新注册，不能恢复已执行命令或重放事件副作用。事件须绑定原注册代际，使用已有订阅／流机制，不新增审批或全局插件轮询。卸载准入先结清可释放贡献引用，其他实际运行／使用引用仍冲突；删除贡献不删除用户配置、快捷键或业务数据。

#### Scenario: 关闭一个窗口而另一窗口仍使用插件
- **WHEN** 两个窗口使用同一 runtime，其中一个关闭，随后插件被明确停用
- **THEN** 先只释放已关闭窗口的注册与订阅，另一个原调用保持；停用后对应命令／菜单／快捷键与监听被撤销，用户保存的数据不被清空

#### Scenario: 声明包卸载与运行包引用冲突
- **WHEN** 用户已解除绑定并结清模板／intent，仅剩可释放 UI 投影时卸载声明包，或卸载另一窗口仍在运行调用的包
- **THEN** 前者结清本窗口投影后可卸载且不残留注册；后者返回真实引用冲突，不通过强杀服务或删除用户配置伪造成功

### Requirement: 未开放 Provider 必须沿同一公共注册边界扩展

后续 Provider SHALL 共用声明／能力协商、注册／释放、明确目标、代际、事件和取消规则，其数据、方法及业务权限由具体扩展点另行定义；不得用任意 method／payload 调度冒充完整 Provider 合同。原项目已接入的搜索、文件、Thread 编辑器和大纲 SHALL 保留各自权威 owner；删除旧宿主接线前必须清点其所有消费者并迁齐受影响的有效业务路径。

#### Scenario: 开放一个后续搜索 Provider
- **WHEN** 后续变更决定开放插件搜索能力
- **THEN** 必须补齐该点的版本、查询／结果／定位／事件合同、原查询 owner 及真实入口验收后才报告支持，不能仅登记名称或借旧符号 RPC 算完成，也不建立第二搜索框架

### Requirement: 源码调试必须迁移现有工具并复用唯一业务 owner

系统 SHALL 将已有 Node Inspector JavaScript 调试的 adapter、Inspector 连接和目标进程执行迁至 workspace 范围 Debug 插件，Backend 经 Gateway 调用，不依赖 js-debug VSIX。Backend 原 owner SHALL 保留配置／活动方案、断点、动作时间线、精确 session_id／thread_id、权限、变量脱敏和审计；原 `/debug/node` API 及 debugging 组20个工具的公开合同保持。Session-only 入口从 catalog 解析真实 main_thread_id，不以别名或当前焦点推断。

Workbench SHALL 沿原 Debug 服务／UI 经 Gateway 进入同一 Backend 调试 owner，再由 Gateway 路由插件；UI 和 Agent 操作同一 run，不创建第二断点集、调试配置权威或调试状态机。Agent 执行本身的暂停／断点／快照继续由原 Backend owner 负责。首批 SHALL 只宣称实际验证的 Node JavaScript 能力，Python 插件运行时不代表 Python 源码调试可用。

同逻辑调试类型 SHALL 沿插件贡献命名空间登记一个原 Debug factory，从冻结的调试 session／run 目标及插件绑定版本分发到原 Backend owner。标签与配置 schema SHALL 按目标版本投影，不因相同 type 合并字段或逐目标覆盖全局 factory；释放一个绑定不得删去其他绑定仍使用的 factory／投影，不伪造扩展描述或新增调试状态 owner。

目标和插件绑定版本 SHALL 在调试发起、首次异步配置解析前冻结，无目标时先沿原选择流程确认。配置解析／变量替换、session 创建、adapter 分发及已支持的 runInTerminal 等 factory 入口 SHALL 沿用该原启动上下文；尚无 session 时不得凭 folder、同名配置或当前焦点猜目标。解析使用原目标 owner 的已支持规则，绑定失效或版本变化明确拒绝，不改投或开放任意用户配置 Provider。

#### Scenario: 调试配置解析期间切换焦点或绑定版本
- **WHEN** B1 发起调试并开始异步配置解析／变量替换，session 尚未构造时用户切焦点至 A1，或 B1 原插件绑定版本发生变化
- **THEN** 前者的解析和后续 adapter 仍使用冻结 B1 上下文，零 A1 参数或变量；后者明确拒绝失效启动，不按新版本或另一目标继续，也不创建错误目标进程

#### Scenario: 同调试类型在两个目标分别运行
- **WHEN** A1 v1／B1 v2 同逻辑调试类型的标签及配置 schema 不同，分别启动调试后解除 A1 绑定
- **THEN** 两个 session 分别使用原目标版本的配置并路由各自 Backend run；A1 解除只释放自身资源，B1 factory／schema／既有 run 保持，旧 A1 句柄不删除 B1 注册

#### Scenario: UI 与 Agent 操作同一 Node 调试运行
- **WHEN** 用户在原 Debug UI 启动工作区 Node 脚本并停在断点，Agent 随后读取变量和继续运行
- **THEN** 两者使用同一 run、断点和时间线；目标进程由插件创建，原工具结果按原 call ID 结算，Gateway 不写调试业务历史，另一 Thread 的 run 不受影响

#### Scenario: Agent 执行暂停不创建源码调试器
- **WHEN** 用户暂停 Agent 并查看其停止快照，或仅浏览调试配置列表
- **THEN** 原业务正常处理且不因此创建源码调试目标；未实现的 Python 调试请求明确拒绝，不回退 VSIX 或 Backend spawn

### Requirement: DAP 必须遵守原调试时序和真实能力边界

自有 Debug 接入 SHALL 处理 initialize／能力协商、launch／configurationDone、源码与断点、threads／stackTrace／scopes／variables、继续／单步／暂停／evaluate、输出及停止／继续／终止／退出事件、错误和取消。初始化顺序、setBreakpoints 按源替换及 logpoint／验证结果 SHALL 映射到同一业务 owner；未实现选项不宣称支持。attach、restart 和 runInTerminal 等可选能力须逐项验证；如提供 runInTerminal，目标只由精确归属的 Terminal 插件创建。

调试 run、服务 instance_id 和暂停代际 SHALL 分离；frameId／variablesReference／sourceReference 等协议句柄限定合法 run 及有效代际，源码 URI／版本沿原资源 owner，不能用远端路径伪造本地 file URI。来自旧暂停状态的操作不能作用于新状态，UI／Agent 竞争动作由同一 owner 决定准入与结果。disconnect、detach 和 terminate SHALL 区分，关闭 UI 不自动终止目标；显式终止／重启核实实际自有目标退出，不强杀未经授权的 attach 外部进程。断连不视为退出，launch／continue／evaluate 结果未知不得自动重放。

#### Scenario: 初始化断点和旧变量引用
- **WHEN** UI 在 configurationDone 前设置源码断点，随后暂停读取变量，再继续并使用旧 variablesReference
- **THEN** 原断点 owner 在正确时序发布真实验证结果，继续后旧引用明确失效，不返回另一暂停或 Thread 的值；源码跳转使用正确目标 URI

#### Scenario: 有副作用求值响应丢失
- **WHEN** evaluate 已使目标计数器递增但响应丢失，之后 Backend 重连原调试服务
- **THEN** 原结果为已知或未知，计数器不因自动重放再次递增；仅按实际能力查询原 run，不假报旧工具执行恢复，显式 stop 后核实自有进程退出

### Requirement: Python 工具条件脚本和视频预处理必须保持现有实现

python_exec、持续任务 script 条件和视频预处理 ffmpeg SHALL 保留原调用方式、Backend 执行位置、进程管理、结果与清理合同，不迁到 Gateway、终端插件或其他受管执行服务。python_exec 的解释器选择、cwd／PYTHONPATH、双流和非零／超时行为，条件脚本的 JSON 输入、原监督器及期限／内存／输出限额、TaskDecisionDTO 验证和任务语义，以及 ffmpeg 的原抽帧、临时文件和错误处理 SHALL 保持。本 change MUST NOT 为这三条链新增非 PTY 服务、适配层、备用分支或迁移测试要求。

#### Scenario: 插件迁移不改变三条原执行链
- **WHEN** 本次插件体系完成迁移后，用户调用 python_exec、触发持续任务条件脚本或提交视频附件
- **THEN** 三者继续走现有 Backend 实现及进程执行链，不请求 Gateway 插件执行接口、不要求终端插件启动，原输入输出和错误处理不变

### Requirement: 工具覆盖盘点不得迁走纯业务 owner 或误删扩展工具

实施 SHALL 按真实 default／agent-collaboration／debugging 注册、动态 MCP／custom 目录和进程调用方完成盘点；保留工具 group_id、名称、schema hash、封存资格、call ID 和 canonical 结果，不能把“扩展工具”当成旧 VS Code 扩展删除。外部 custom 执行 SHALL 通过显式 MCP 或具名 typed 服务，不能保留 Backend 子进程旁路，也不强制把全部内置工具改名或转成 MCP。

文件／补丁／搜索、技能、HTTP 查询、Goal／任务编排、Agent 协作及执行调试 SHALL 保留原业务 owner；外部执行只迁移本 change 明确纳入的链，python_exec／条件脚本／ffmpeg 保持原实现。SQLite 快照隔离进程属于存储内部一致性原语，本次保留，不交 Gateway 数据库路径；系统文件管理器显示属于宿主动作，暂不迁插件、不新增 Electron 原生能力、不顺带删除原行为。Launcher／SSH／后端生命周期沿原控制面管理，async task 不当作 OS 子进程。验收 SHALL 明确覆盖工具及外围执行链，不宣称 Backend 所有进程皆已移除。

#### Scenario: 纯业务工具不产生无必要服务
- **WHEN** Agent 调用文件搜索、协作、技能或 Goal 工具，并执行 builtin 条件或模型 judge
- **THEN** 原 owner 保持业务行为及身份，没有因统一插件体系新起服务或复制索引／任务状态；动态工具按原目录资格处理，不因“扩展”名称失去注册

### Requirement: 插件必须能声明携带 Skill 而无需运行进程

plugins[].contributes.skills SHALL 为包含稳定局部 id、包内相对目录 path 和可选 tool_bindings 的声明数组；目录包含 SKILL.md 及配套文件。名称／说明和正文 SHALL 由原 frontmatter／Skill 派生 owner 取得，不在 manifest 重复维护。安装发布前校验文件存在、路径不越包根、来源内名称唯一及原格式约束；未知贡献版本或非法资源明确拒绝。纯 Skill 插件 SHALL 不要求 runtime／管理 ready，浏览、读取或加载 Skill MUST NOT 执行包内脚本、启动服务或授予工具权限。

#### Scenario: 无 runtime 的 Skill 包
- **WHEN** 用户安装仅含合法 Skill 和配套示例的本地包并在明确范围启用，再读取文件及调用 skill_load
- **THEN** 安装、目录发布和原技能激活均可完成，零插件进程启动，普通文件读取不激活；非法目录或重复来源名称在发布前明确拒绝

### Requirement: 插件 Skill 必须进入现有目录并隔离目标范围

Skill 包文件 SHALL 由原生目标唯一安装 owner 持有，按明确插件启用绑定通过原 Gateway Skill 来源及认证资源链提供，Backend 原 SkillCatalog 统一派生。系统 SHALL 沿 workspace > gateway > network > inline 优先级，保留真实来源、包版本／摘要、贡献身份及原资源 revision，不新增来源层、第二技能目录／索引或向工作区复制包。同层插件贡献名称冲突 SHALL 明确拒绝候选并保留原发布，不按安装顺序覆盖或自动改名。workspace 绑定不向其他工作区披露，Relay 不复制源；同名高层覆盖保留真实来源，不假报包来源或仅凭名称认定工具合同匹配。

安装不自动向工作区发布贡献；服务实例范围不代替 Skill 可见范围。资源读取、Skill 激活、工具执行和运行管理 SHALL 各走原 owner 权限。插件关闭撤销其新 Skill 使用，崩溃／重启不等于关闭静态说明；读取已开启插件的说明无需运行管理权。前端和模型 SHALL 使用正式 Skill URI，物理安装路径、端口和进程身份不得泄漏。

#### Scenario: 工作区隔离与同名覆盖
- **WHEN** A1 启用插件 Skill，A2 未启用，且 A1 有同名 workspace Skill 覆盖包贡献
- **THEN** A2 不获得该 workspace 绑定资源；A1 按原优先级解析 workspace 来源并显示真实 provenance，包关联不得把覆盖内容报告为自己的已匹配版本

#### Scenario: Relay 读取无需启动插件
- **WHEN** A 经 Relay 读取 B 工作区已启用的 Skill，而 B 的服务未启动
- **THEN** 读取经 B 原生资源 owner 的权限完成且保持 B 来源，零进程激活、零 A 副本，缺少披露权限则明确拒绝

#### Scenario: 两个插件声明同层同名 Skill
- **WHEN** 已启用插件发布某 Skill 后，另一插件尝试在相同层和可见范围发布同名贡献
- **THEN** 原 owner 明确报告冲突并拒绝该候选，原 Skill 继续可用，不按安装先后覆盖或静默改名

### Requirement: Skill 与工具的关联不得改变工具准入和调用合同

tool_bindings SHALL 明确稳定 tool_id、服务贡献引用（服务工具时）及支持的工具合同版本，复用原目录版本／schema hash 标识而不新增版本登记，只能关联本插件服务导出或产品允许的既有工具。安装只检查声明及包内引用，不启动服务、等待 MCP 目录或远程探测工具；运行关联由原目录状态决定。原工具目录 owner SHALL 将插件内默认关联与 tools.custom[].skills 显式关联按真实来源身份归并去重，不回写用户配置或新建工具目录；关联只用于说明／发现／归因，不注册、启用或执行工具，不自动加载 Skill。工具缺失、实例失效或合同不兼容 SHALL 明确标注关联不可用，不能改投同名工具或禁止合法诊断性 Skill 读取。

search_tools／invoke_extension_tool、稳定工具名／参数 schema、group_id、封存资格、call ID、canonical 结果及前端 skill_names 归因 SHALL 保持原合同。SKILL.md 的 allowed-tools MUST NOT 重新成为授权、工具注册或自动 allowlist 来源；正文不能覆盖工具策略或代授运行管理权限。

#### Scenario: 加载说明不取得执行权限
- **WHEN** 主体可以读取／加载某 Skill，但对应工具被现有策略禁止或服务启动未获准
- **THEN** 原 Skill 加载可完成，工具调用或启动按原 owner 明确拒绝；allowed-tools 和包内关联不能改变准入，安装命令不会变成 Agent 工具

#### Scenario: 工具关联版本不匹配
- **WHEN** Skill 声明支持工具合同 v2，而当前绑定只提供 v1 或目录尚未发布该工具
- **THEN** 展示明确的不兼容或不可用原因，不按相似名称自动绑定、不新起另一目录，原 v1 工具是否可调用仍由其原策略决定

### Requirement: Skill 更新和卸载必须遵守原冻结上下文生命周期

skill_load SHALL 保留 name-only、snapshot／tracked／untrack 及本次 Turn／ModelCall 冻结 activation snapshot 语义；普通文件读取不激活／追踪，正文和来源由原上下文 owner 注入并封存。安装新包版本不改变现有插件贡献／运行绑定，明确切换插件绑定的包版本后才由原来源 owner 发布新 revision，包内资源及工具合同关联在同一候选校验后可见。切换 SHALL 按原 owner 返回实际版本与逐项结果，UI 显示部分生效及错误；仍残留的旧贡献仅作诊断，不冒充新版本，依赖贡献／运行版本一致的新调用或 Skill 使用在不匹配时明确不可用，不增加第二事实源或跨 owner 事务，不要求失败后重建旧注册。已冻结调用不得改读新包，snapshot 不自动更新，tracked 只沿原观察／接受时序更新，untrack 不移除已注入内容；不可用版本／冻结冲突明确报错。

停用、撤权、解除绑定或卸载 SHALL 撤销对应的新发现／新读取／新加载及观察准入，保留会话已封存正文、来源、历史和用户配置。真实未完成读取／运行引用仍按原 owner 阻止删除，引用结清后删除包；已完整封存正文不新增永久包引用或持久账本。模型准备及 skill_load SHALL 不附带新远程发现、全包 hash、进程探活或全量工具目录刷新，普通目录／读取沿原总延迟预算。

#### Scenario: 更新不改写冻结调用
- **WHEN** Thread 已冻结 v1 Skill，用户安装 v2，再明确切换插件绑定的包版本并进行后续 snapshot／tracked 加载
- **THEN** 已冻结调用继续使用原 v1 正文与工具资格，新加载按各自冻结快照及原追踪规则处理 v2，有代码插件切换版本先核实旧实例退出再启动 v2，静态说明随同一包版本发布，不用 v2 描述冒充 v1 运行能力

#### Scenario: 停用和有引用卸载
- **WHEN** 包有未完成的合法配套文件读取时被请求卸载，随后读取结清、插件关闭且原绑定／模板／intent 引用均解除
- **THEN** 先报告真实引用冲突，结清后可删除包，新发现／读取／加载及观察准入撤销；已封存 Skill 正文、来源和工具历史仍可查看，不因历史引用永久阻止卸载

### Requirement: 产品 Skill 必须与受影响工具纵向迁移

browser-control 与 debugging SHALL 随 Browser／Debug 能力迁齐 Skill 正文、配套文件、工具 schema／示例、bundled groups、归因展示、测试和发行引用，保持原 Skill name 与工具调用信封；切换后物理移除旧 inline 同义发布及 fallback。合法 workspace 覆盖与历史冻结来源继续由原 owner 处理，不保留开发路径双读。web-search-fetch、gateway-context SHALL 保留原产品资源和业务 owner，不因 Skill 插件化改成进程服务；python_exec、持续任务条件脚本及 ffmpeg 原执行和关联不改。

#### Scenario: 浏览器和调试技能经真实入口使用
- **WHEN** 通过完整 Gateway／Workbench 初始化，在 Thread 中加载迁移后的 browser-control 或 debugging 并经原工具信封执行
- **THEN** 加载的说明和示例匹配原稳定工具 schema，操作进入精确插件实例且结果沿原 call ID 封存；旧 inline 同义来源不可用，其余两组 Skill 继续由原 owner 提供

### Requirement: 普通工具调用必须只执行必要边界检查

系统 SHALL 在安装／配置变化时验证包和插件，在注册／目录变化时由原 owner 校验工具定义并复用不可变 schema 身份。暖调用只核当前禁用状态、参数、冻结目标与有效注册、剩余期限，保留原 call ID／撤权／结果结算；同一 schema 不逐调用重复导出或 hash，同一合同不跨层重复完整校验。Gateway 负责认证与插件实例范围路由，Backend 负责业务准入；普通调用不要求安装／启停权限，不探活进程、全包审计或刷新全目录，遵守既有完整交互总预算。

#### Scenario: 已运行插件的连续工具调用
- **WHEN** 对已 ready 且定义未变化的插件连续发起合法工具调用
- **THEN** 复用原注册和连接，无启动请求、全包 hash、重复 schema 导出或进程扫描；每次仍拒绝非法参数／错误归属／已撤权目标，总延迟按真实结果计量
