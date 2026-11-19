## Purpose

定义自有受管服务插件的来源、安装与 MCP 使用关系，确保公网分发尚未建设时可明确展示状态并使用本地包，安装、工作区启用、标准 MCP 连接和工具执行始终具有独立且真实的归属，不再依赖第三方插件市场或 VSIX Provider。

## ADDED Requirements

### Requirement: 插件来源必须采用自有协议且未配置时零外部查询

插件目录与下载 SHALL 使用项目自有的 source_id／协议版本、分页／目录版本、插件确切版本、目标平台及运行时／ABI 兼容信息、完整包地址、标准 `.zip` 文件名与摘要合同；文件名不参与插件身份或兼容性判定。当前只预留可配置自有公网来源，不提供虚构默认地址、在线市场或第三方目录 fallback。未配置或未启用来源 SHALL 明确显示“插件源尚未配置”，同时允许本地完整自有包、已准备的显式开发目录和已安装管理。

查询 SHALL 绑定明确来源与目标，输入或来源变化时取消／废弃旧结果；无命中只显示无命中，不自动扩大来源。未知目录版本明确拒绝，不调用 VS Code Marketplace、Open VSX 或旧 MCP Registry 适配器。

#### Scenario: 无公网源时使用本地包
- **WHEN** 用户打开管理 UI 且未配置自有公网源，再安装合法本地插件包
- **THEN** 市场明确不可用且零外部查询，本地安装和库存可用，不因缺少市场启动失败或生成假目录

#### Scenario: 来源变化与未知协议
- **WHEN** A 来源请求未完成时切换 B，或目录返回未知协议版本
- **THEN** A 的迟到结果不进入 B，未知协议明确报错，不改查第三方或本机缓存伪装在线结果

### Requirement: 包安装必须冻结目标版本并由唯一目标 owner 发布

安装 SHALL 冻结原生 Gateway、安装域、来源、package_id、确切版本、兼容目标制品和摘要，检查 manifest、目标平台／发行运行时／ABI、完整运行依赖及安全归档路径，安全解包后原子发布。依赖及原生模块 SHALL 在发布构建阶段准备；安装不执行包管理器、编译或联网补依赖，不要求用户安装开发环境。安装 owner SHALL 只有一份发布／操作记录；多个前端、Backend 和 Relay 读取同一事实，不按客户端或工作区复制安装库存。

安装步骤使用原幂等操作身份，取消／响应中断后查询原操作结果，不自动重放未知安装。Gateway 与 Backend SHALL NOT 在用户安装阶段发起 Python／Node 依赖准备或编译子进程。安装／更新只发布版本，不修改插件绑定、模板或实例；普通重启保持绑定版本，切换插件绑定的包版本是独立配置操作。切换 SHALL 先由原 owner 完整预校验，包括确切版本、目标制品及运行时兼容性，再由原 writer CAS 提交期望并撤新使用准入；已启用代码插件停旧核退出再启新，已关闭插件不启动，纯声明跳过进程步骤。贡献按固定引用顺序替换，预校验失败保留原发布；已释放旧句柄后的登记失败只清本次新增句柄，停止后续发布并报告真实结果，不重建旧句柄。新启动失败不自动恢复旧版，已提交期望不自动回滚，不创建贡献独立版本或跨 owner 事务。模板、未结清创建 intent 和原 active／pending 绑定均阻止旧版本自动清理或卸载；引用检查限原实例、绑定、模板／intent、读取／使用 lease，未知来源拒绝本次卸载，不新增全域扫描。包摘要验证不得进入每次普通列表／工具调用。

缺少对应确切版本、目标平台制品或所需运行时／ABI 时 SHALL 不生效，原生 Gateway 返回具体缺失／差异及获取兼容完整包或受支持应用版本的提示；Relay 使用目标机器的实际环境，不用发起端平台选包。预检失败不改变原绑定或停止仍可用的旧实例，不自动替代版本、现场下载环境或编译，沿 multi-target-extension-hosts 的完整制品合同执行。

包内 plugins[].contributes.skills 资源与工具合同关联 SHALL 随同版本安装校验和原子发布；安装不自动向工作区启用贡献或注入 Skill，纯 Skill 插件无运行时依赖。需要配套说明的 MCP 使用 SHALL 沿原 Skill 来源／SkillCatalog／skill_load 和工具目录 owner 关联，不将 MCP prompts 当作已激活 Skill，不为 Skill 加载另建 MCP client 或绕过原工具策略。

#### Scenario: 安装响应丢失与共享包
- **WHEN** 同一目标安装成功后响应丢失，另一客户端查询，两个工作区随后使用此包
- **THEN** 查询同一原操作与唯一发布记录，不产生第二安装；工作区是否启用、参数、秘密和运行实例另行决定

#### Scenario: 经 Relay 为缺少对应版本的目标安装
- **WHEN** Linux 客户端经 A 为 Windows 目标 B 安装某确切版本，但该版本只有 Linux 制品或不匹配 B 的发行运行时
- **THEN** B 明确拒绝并返回所需／可用平台或版本，A 展示原目标原因；B 零新安装生效或旧绑定切换，A 不改用本机制品，也不提示用户安装编译环境

#### Scenario: 同槽位版本切换及纯声明更新
- **WHEN** 用户先安装 v2，再切换已开启代码插件和纯声明插件的绑定版本
- **THEN** 安装阶段原绑定不变，代码插件在同一槽位停旧核退出再启 v2，零并行第二实例；纯声明插件经原 owner 预校验后替换发布而零进程，预校验失败保留原发布、实际登记失败只清本次新增句柄，不假报全部生效或自动恢复旧版

#### Scenario: 版本切换跨 owner 部分失败
- **WHEN** 插件 runtime 已切到 v2，而后续静态贡献 owner 在实际登记阶段失败
- **THEN** 清理本次新增注册、停止后续发布并返回各 owner 真实版本及成功／失败，UI 展示部分生效和原错误；不重建已释放 v1 句柄，仍残留的旧声明不冒充 v2，需要版本一致的新调用或 Skill 使用明确不可用，已冻结历史保持，不自动回滚／重放或创建新账本

#### Scenario: MCP 包附带 Skill 但尚未启用
- **WHEN** 用户安装带有合法工具说明和 tool_bindings 的 MCP 包，但未对 A1 开启对应插件
- **THEN** 版本及说明资源安装成功，A1 不自动获得新 Skill、工具或运行实例；后续显式插件开启和 Skill 加载仍经各自原 owner 准入

### Requirement: MCP 使用必须显式绑定工作区和插件实例

MCP 服务 SHALL 采用项目插件合同，stdio 由目标插件持有，Backend 经 Gateway 的标准 Streamable HTTP 连接；外部原生 HTTP MCP SHALL 经同一 Gateway 路由且无需本地包装进程。配置绑定 package_id／plugin_id、service_id、确切包版本或明确外部端点和工作区，不能回退 Backend command／args 启动或 VSIX 动态 Provider。

工作区插件启用及参数修改 SHALL 经原 source writer、预检、CAS、候选和 publication；安装、来源保存、连接 ready 与 Agent 可调用分别报告。工作区私有输入继续由原 private-input owner 保存和解引用，仅经认证准入交目标实例，Gateway 不存工作区秘密；OAuth 继续由原工作区 owner 处理。

#### Scenario: 安装后启用并实际调用
- **WHEN** 用户安装自有 MCP 插件，再在 A1 通过来源 CAS 启用并从真实 Thread 调用
- **THEN** 安装后但配置提交前 A1 目录保持，提交成功后经 Gateway 到精确实例执行并写原调用结果；A2 不自动启用或取得 A1 凭据

#### Scenario: 外部 HTTP MCP 不创建包装子进程
- **WHEN** 工作区显式配置一个已有的原生 HTTP MCP 服务
- **THEN** Backend 经 Gateway 到该服务，保留原授权和 MCP 会话合同，Gateway 不额外创建无业务作用的宿主进程

### Requirement: MCP 目录及执行必须继续使用原工作区 owner

管理 UI、快速访问和 Agent SHALL 读取同一工作区 MCP owner 的完整目录、readiness、revision、connection generation、调用租约及操作结果。Gateway 和插件管理层不得建立第二工具目录或调用历史。服务存活、包已安装或 HTTP 成功不表示工具已执行；旧封存调用不得按同名工具或新实例迁投。

#### Scenario: 两个入口查看同一服务
- **WHEN** 用户从插件管理与原会话资源入口查看同一 MCP，并在运行中更新包
- **THEN** 两处反映同一工作区业务事实，原调用保持原连接／版本／期限，更新不自动改写已冻结调用

### Requirement: MCP 管理不得继续依赖 VSIX 服务提供者

用户包 SHALL 通过自有 manifest 和具名服务配置声明能力，不注册 vscode 的 MCP definition Provider、解析回调或 Extension Host 生命周期。旧 Provider 来源、Registry variant 与 Backend managed launch 消费者 SHALL 迁齐后删除；未知旧定义明确拒绝，不用同名包自动替换。自有格式之外的历史数据只根据真实 release 支持范围提供显式迁移，不保留运行时双读。

#### Scenario: 旧 Provider 或 command 配置不能旁路新边界
- **WHEN** 用户提交旧 VSIX Provider 引用、第三方目录安装计划或 Backend stdio command
- **THEN** 返回明确的不支持格式／配置错误，没有旧宿主、目录请求或 Backend 子进程；可支持数据升级只通过显式原 owner 迁移

### Requirement: 插件管理权限不得代授工作区配置与业务访问

安装、服务运行管理、配置读写和实际工作区工具／资源使用 SHALL 按各自原 owner 核验；目标原生 Gateway 最终判权，Relay 只转送。普通发现／读取不授权安装或重启；有安装权限也不能写工作区配置、读取秘密或调用任意服务。

#### Scenario: 只有安装权限的用户
- **WHEN** 用户能够安装自有 MCP 包，却尝试修改工作区配置、启动／重启服务或读取 A1 私有输入
- **THEN** 各入口拒绝缺少的对应权限，已安装包仍真实存在，不伪报业务启用成功

### Requirement: MCP 目录和附带 Skill 必须随插件开关收敛

已开启 MCP 插件 SHALL 由 Gateway 在所属范围运行时启动，Backend 原 MCP owner 完成连接／完整目录验证后才可执行；工具发现和信封调用不启动插件。关闭停止新调用并由原 owner 撤销该绑定目录资格及新 Skill 使用，在原总期限内结束在途连接／操作后由插件核实自有进程退出；已发送副作用按原结果或未知结算，不重放。gateway 插件关闭作用于其全局实例，workspace 插件关闭仅影响对应工作区，UI 不混同。

#### Scenario: 关闭一个工作区的 MCP 插件
- **WHEN** A1／A2 使用同包的独立 workspace MCP 插件，用户关闭 A1
- **THEN** A1 新目录／执行及 Skill 使用准入撤销、原操作按真实结果结算，自有进程退出核实；A2 的目录和实例不变，A1 新请求不自动启动
