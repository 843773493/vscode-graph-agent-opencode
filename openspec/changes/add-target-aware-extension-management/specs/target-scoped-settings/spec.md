## Purpose

定义复用 Code OSS 设置交互来编辑项目自有插件、Gateway 和 Workspace 配置的统一体验，保持明确目标、原来源层和唯一写入 owner；不提供第三方 vscode 配置 API，不把插件服务状态、客户端展示态或工作区业务秘密混成一份配置。

## ADDED Requirements

### Requirement: 设置编辑必须持续展示并冻结明确目标

设置 UI SHALL 复用原设置编辑器交互，在顶部复用插件管理的 Gateway 目标动作，各视图独立保留选择，初次默认当前入口原生 Gateway。读取／保存 SHALL 绑定编辑对象的目标、插件贡献、必要服务及工作区／来源层，不被其它视图或编辑器焦点改写；纯声明插件不要求服务实例。目标切换 SHALL 沿原 dirty 编辑流程保留或结清草稿，不将旧草稿写入新目标；默认值只读，只在合法覆盖层修改。

#### Scenario: 编辑 B 设置后切换其它管理视图目标
- **WHEN** B 的设置编辑器有未提交内容，用户将插件列表目标切为 A，再回到设置保存
- **THEN** 保存仍指向 B 原来源，界面持续显示归属；B 失效则明确失败，不改投 A 或重新逐项询问目标

### Requirement: 插件设置必须来自自有 schema 并提交原配置 owner

插件 manifest 的 `plugins[].contributes.configuration` SHALL 是配置 schema 的唯一包内来源，声明 package_id／plugin_id 命名空间和插件固定 gateway／workspace 范围、默认值、秘密输入及生效分类，字段进入原 Gateway 或精确 Workspace 配置命名空间，不读取 VSIX contributes.configuration、不提供 vscode getConfiguration／inspect／update 桥。纯 Workbench 主题／布局／快捷键仍归原窗口 profile；不能因 UI 相同就同步写插件业务配置。schema 注册与读取不要求 runtime 启动，运行期处理者不注册另一份有效配置。

已安装 schema 的查询结果及 UI 投影 SHALL 携带原生 Gateway、必要 workspace_id、package_id／plugin_id 和确切包版本，并在冻结的设置目标上下文解释。同一逻辑插件在不同目标使用不同版本时，不能压平成 Workbench 单例全局 schema 而互相覆盖字段／默认值；继续由原配置 owner 提供当前目标投影，不新建第二配置 registry。

插件参数 SHALL 位于原配置 owner 正式登记的 package_id／plugin_id 参数容器，与引用确切版本的运行绑定分离。卸载前必须解除原 active／pending 绑定；未绑定或未安装插件的参数只在原来源保留并标为未生效，不参与 effective／active／pending 或热应用、不计包引用。无包 schema 时不提供参数表单，原 JSONC／source writer 仅按已登记容器结构和 JSON 类型支持查看、编辑与删除。重新显式绑定前 SHALL 按确切包版本 schema 完整校验参数，失败不启用；此有限容器外的未知字段及已绑定插件的非法参数仍拒绝，不保留影子 schema、孤儿参数账本或另一配置文件。

表单和 JSONC SHALL 共用原 source writer、预检、CAS、候选及 active／pending，原配置合并顺序保持，只有已登记字段可热应用；重启服务实例与重启 Backend 分别说明。没有稳定来源文件的对象不编造 profile／view 文件；旧用户 Extension Host 的 settings／多根 view 接口迁齐后删除，不保留第二有效配置。

#### Scenario: 同包两个 workspace 参数独立
- **WHEN** A1/A2 使用同一包并在设置 UI 保存不同参数或删除一项覆盖
- **THEN** 分别进入原 workspace 来源和 CAS，默认值由原合并规则暴露，不修改另一工作区、Gateway 或 Workbench 展示态

#### Scenario: 纯声明配置插件停用
- **WHEN** 无运行入口的插件贡献合法配置 schema，用户保存覆盖值后关闭该插件并继续编辑配置
- **THEN** 已安装版本的 schema 保持可读，配置编辑及保存由原 owner 完成而无进程，不恢复命令／工具／Skill 新使用，未授权来源仍不可读取

#### Scenario: 整包卸载后保留配置来源
- **WHEN** 用户已解除原 active／pending 绑定并结清其他引用，卸载曾贡献配置 schema 的插件包
- **THEN** 释放对应 schema 投影，原参数容器中的保存值保留但未生效且不阻卸载；原 JSONC／source writer 仍可按登记容器结构编辑与删除，不依赖已删除包提供表单，不保留影子 schema 或放宽域外未知字段校验

#### Scenario: 参数残留重新绑定及域外错误
- **WHEN** 用户保留未绑定插件的参数后显式绑定新版本，而残留不符合新 schema；或在参数容器外提交未知字段
- **THEN** 前者按确切版本 schema 拒绝绑定且零启动，残留仍未生效；后者按原 schema 拒绝提交，不以允许保留残留为由接受任意未知配置

#### Scenario: 同插件不同目标使用不同 schema 版本
- **WHEN** A1 的插件 v1 与 B1 的同逻辑插件 v2 声明同名但类型或默认值不同的字段，用户在 B1 设置中查询和保存
- **THEN** 查询、表单、校验及提交均使用 B1 v2 的合法 schema 与来源，结果保留目标及版本身份；A1 v1 不被覆盖，迟到 A1 结果不改变 B1 字段解释

### Requirement: 通用插件秘密参数必须支持密码输入与目标受保护存储

插件秘密参数 SHALL 支持用户在原设置表单／Quick Input 的密码控件中直接输入，确认后由所属 Gateway／Workspace 配置 owner 沿现有私有文件权限／ACL 与原子发布保存不可变值，并只返回正式登记的插件参数引用。输入与保存 SHALL 冻结原生目标、必要 workspace、来源层、package_id／plugin_id、参数位置及校验使用的确切包版本。引用再绑定不可变值版本，不能直接复用 MCP Server 专用引用或接受任意文件路径。首版不要求新增系统密钥链、通用 SecretStorage SDK、秘密账本或跨机器同步，不得将权限受限文件称为已经具备系统加密存储。

writeOnly SHALL 在本项目驱动秘密输入及禁止回显：专用受保护写入口允许接收真实值并核原 configuration_write，普通 JSONC／参数通道只接受合法私有引用或既有显式环境引用，拒绝秘密明文。普通配置读取只能返回引用及配置状态，不提供回读原值接口，安装或运行管理权限不代授秘密访问。真实值按确切包版本 schema 验证，错误不得携带输入正文；不能按字段名猜测秘密，或因普通正文包含类似引用的文本而自动解引用。

环境变量 SHALL 作为可选来源保留，沿本项目既有 `${ENV_NAME}`／`env:NAME` 合同由配置所属目标解释，不读取浏览器／Relay 环境，不为对齐 VS Code 增加另一语法。私有引用失效不得静默改用环境值，环境缺失也不得借同名秘密或其它目标补齐。

秘密写入成功后，引用 SHALL 继续经原 source writer、预检、CAS、候选及 active／pending 提交；写入私有文件不代表配置已生效或插件已启动。更换创建新不可变值版本，再按原 CAS 切换引用；清除经原 writer 只移除选定层覆盖并揭示下层值，不能把遮罩写回或删除其它层及仍被 active／pending 引用的正文。未引用值按原私有输入 owner 清理规则处理，插件卸载不顺带删除秘密。所有输入交互确认前取消 SHALL 不写秘密或来源，CAS 冲突保留原配置；真实输入不得保存为浏览器持久草稿或进入普通日志、事件、诊断。

工作区原 owner SHALL 在已认证启动／配置应用交付中仅将实际值传给对应插件；Gateway 可以认证转送，但不得读取工作区私有文件或在接管记录、普通配置镜像中持久化原值。已保存合法输入 SHALL 在显式重建时复用，无需重复询问；正常接管保留已核实实例。缺输入明确等待并提供显式设置入口，缺失版本、权限或归属错误保留原因；状态读取和后台恢复不自动弹框，不改投其它机器或重放业务。更换／清除沿原生效分类处理，不保证运行实例立即忘记已取得的值。

#### Scenario: 两种插件范围通过密码输入保存并在重建后复用
- **WHEN** 用户分别为 Gateway 插件和 A1 workspace 插件输入秘密，经原来源 CAS 保存并启动，随后显式重建实例
- **THEN** 两个插件只取得各自值，重建从原 owner 复用已保存引用而不重复提示；普通来源、事件及接管材料均无原值，Gateway 不读取 A1 私有文件

#### Scenario: writeOnly 接受受保护输入而拒绝普通配置明文
- **WHEN** 用户通过密码输入保存 writeOnly 字段，或将同一真实值直接放入普通 JSONC／参数请求
- **THEN** 前者经授权保存并只返回引用，后者在配置提交前拒绝且不回显正文；读取设置只显示配置状态／引用，不用真实值填回输入框

#### Scenario: 取消输入及更换时的 CAS 冲突
- **WHEN** 用户在密码输入确认前取消，或已准备新私有版本但引用提交遇到并发来源变更
- **THEN** 取消不写秘密或来源；冲突保留旧引用和原配置，新私有版本不被当作生效或自动重试保存，不覆盖并发更改

#### Scenario: 清除覆盖保留真实来源和在用版本
- **WHEN** 用户清除选定层秘密覆盖，且下层有另一引用或原 active／pending 仍引用旧值
- **THEN** 原 writer 按来源规则提交删除并显示下层或未配置状态，不删除其它层和仍在用的秘密正文；实例变更按原配置生效分类报告，不伪报运行中已擦除原值

#### Scenario: 显式环境引用只在配置所属目标解析
- **WHEN** A 经 Relay 为 B1 配置显式环境引用，A 与 B1 的同名变量值不同，或 B1 缺少该变量
- **THEN** 插件仅取得 B1 原配置 owner 解析的值，缺失时明确报告输入问题，不读取 A 的环境、复制为私有文件或静默选择其它来源

#### Scenario: 引用归属错误与私有版本缺失
- **WHEN** A2 或另一个插件／参数字段使用 A1 的秘密引用，或显式重建时原私有版本缺失
- **THEN** 原 owner 拒绝错误输入并报告不含秘密正文的原因，零错误目标交付；不自动弹框、回退环境／本机或重放业务调用

### Requirement: 设置发现与读取不得隐式启动插件或 Backend

设置入口 SHALL 从原登记和配置 owner 发现合法目标、来源、已安装 schema 及可写能力，不以插件 ready／运行权限为前提，不为普通读取启动服务或 Backend。发现只披露配置权限允许的稳定身份；实际 Workspace 来源读取仍须其原 owner 可达，离线或缺少 writer 如实说明，不让 Gateway 越界读取工作区文件、不返回虚构完整配置或借安装权限代授配置读取。用户显式启动 Backend 是独立生命周期操作。

#### Scenario: 只有配置权限且插件已停止
- **WHEN** 用户打开已停止插件的合法 Gateway 配置来源，具有 configuration_read 而无服务运行权限
- **THEN** 可读取已授权 schema／脱敏来源而不启动插件；写入仍独立核 configuration_write，不请求旧宿主 profile 或运行态

### Requirement: 设置归属必须复用原 owner 而非统一成新配置域

Workbench 展示设置、Gateway runtime 配置、Workspace 配置、会话设置、Gateway 用户视图和 registry 手动操作 SHALL 保留各自权威来源及提交语义。统一设置 UI 只导航／编辑合法对象，MUST NOT 将非配置状态伪装成 runtime candidate，或增加跨 Gateway 自动继承／同步规则。业务配置更改不得只写前端状态。

#### Scenario: 布局与工作区默认 Agent 分别修改
- **WHEN** 用户修改当前 profile 的布局，再修改 B1 的 default_agent
- **THEN** 两者分别写原展示态和 B1 配置 owner，不互相产生对方的配置提交；既有会话继续遵守原 Agent 绑定规则

### Requirement: 来源编辑必须补齐原配置 owner 的真实读写合同

现有 sources 元数据或有限 runtime_override PATCH MUST NOT 被当作完整来源编辑能力。系统 SHALL 在原 Gateway／Workspace 配置 owner 增补合法可编辑层发现、脱敏来源正文、schema／effective／遮蔽信息读取、来源预检和 CAS writer，并迁齐正式 owner delta、协议及消费者。正文读取的脱敏占位不得被当作密码原值写回；没有 writer 的层 SHALL 明确只读，不经通用文件 API 或任意 JSON 路径写入绕过 owner。

原单 workspace 更新的 SQLite 物化合同 SHALL 保持不变；显式来源文件编辑经该 source writer 的文件 CAS／原子写入和原导入／fan-out，不由 runtime 更新顺带回写 JSONC。来源保存、层物化和 active 生效 SHALL 分别报告，不形成双写事实源。能力未完成时只读是阶段诊断，不满足本稿所列合法来源编辑的最终验收。

#### Scenario: 从来源摘要打开完整设置编辑
- **WHEN** 用户打开 B 的 Gateway user JSONC 和 B1 的 workspace JSONC，提交一个现有 runtime PATCH 不支持的合法字段
- **THEN** 通过各自正式 source 读写合同完成 schema／CAS／候选闭环，或明确说明该层尚不可编辑；不得把来源摘要当正文、放宽原有限 PATCH 或旁路改文件后伪报成功

### Requirement: 有效设置必须揭示完整来源及遮蔽关系

项目业务设置 SHALL 展示字段所属域、source layer、原来源资源、revision／digest、最终有效值、遮蔽关系、active／pending 和实际生效分类；Workbench 展示设置沿原配置模型；插件设置消费原业务 owner 的实际候选，不伪造 inspect 字段。合并顺序、对象合并及数组替换 SHALL 沿各自原配置合同，不由前端另算一套有效值。API 只物化来源层而未回写文件时必须如实表达；秘密值不得包含在普通诊断中。

#### Scenario: 用户默认值被工作区覆盖
- **WHEN** 同字段在 inline、user、user_local 与 B1 workspace 层分别存在
- **THEN** 显示每层来源及 B1 最终有效层，修改被遮蔽的 user 值不伪报 B1 active 已改变；不丢失 user_local 的独立 revision

### Requirement: 表单与 JSONC 必须共用原候选提交及并发合同

项目业务配置的表单和 JSONC 编辑 SHALL 经相同 schema／预检、来源层和 active 基线、幂等提交及候选流程，不使用通用文件保存绕过配置 owner。Workbench 展示设置 SHALL 使用原 configuration editing owner；自有插件配置使用其 Gateway／Workspace owner。成功须以相应 owner 完整结果替换镜像，冲突／失败须重新读取并保留可处理的编辑内容，不能自动覆盖并发更改。删除覆盖只移除所选层键；格式及注释沿原 writer 处理。

#### Scenario: 表单与 JSONC 并发修改同层
- **WHEN** 两个编辑器读取同一基线，一个先提交成功，另一个以旧基线保存
- **THEN** 后者收到明确冲突及最新来源，不静默覆盖；删除覆盖时显示重新暴露的有效值，而非把默认值写入该层

### Requirement: 配置保存与运行生效必须分别展示

项目业务设置 UI SHALL 显示原 owner 的候选／提交身份、已应用及延后路径、active／pending、错误与重启要求；Workbench 展示设置保留原 reload／应用状态，服务插件展示实际热应用或实例重启要求。仅配置保存或 HTTP 成功 MUST NOT 作为资源已生效证明。重启、重试或丢弃 pending SHALL 使用原管理生命周期和基线，不重放已执行副作用；未登记字段不得被 UI 自行归类为可热应用。

#### Scenario: 配置已保存但需要后端重启
- **WHEN** 有效候选含无法安全热替换字段
- **THEN** 显示真实 pending 及 restart required，保留原 active；仅在匹配实际启动／健康证明后更新生效状态

### Requirement: Workspace 用户来源修改必须显示实际 fan-out

用户级 Workspace source SHALL 只影响该目标环境原合同覆盖的工作区，提交前展示影响范围；各 workspace 保留独立 CAS、候选、revision 与结果。部分冲突及停止工作区追赶 SHALL 明确展示，不伪造跨工作区事务、不自动扩大到其它 Gateway 的默认值。

#### Scenario: 用户 source 更新发生部分冲突
- **WHEN** B 环境的用户 source 更新影响 B1/B2，B1 成功、B2 冲突，B3 已停止
- **THEN** 展示同一 fan-out 的独立结果与 B3 待追赶状态，不回滚 B1、不伪报全部成功，A 环境配置不被修改

### Requirement: 远端配置必须在远端授权并持有唯一有效事实

远端设置 SHALL 由远端原生 owner 最终判权、验证和提交，本地 Gateway 只保存原连接登记及允许的展示投影，不保存另一份可生效远端配置。读取／配对能力不自动授予配置写入。写入中断后 SHALL 查询原幂等提交身份，不能以新身份自动重放；无法确认时表达待核实。凭据保持目标侧私有，不进入配置镜像、普通日志或快速访问历史。

新管理合同 SHALL 在现有联邦 policy 登记默认拒绝、绑定认证主体的 configuration_read／configuration_write：read 覆盖脱敏 schema／source／effective／pending／提交状态与事件披露；write 覆盖来源预检、候选／提交／删除覆盖及 pending 管理，所需读取另核 read。重启仍核原生命周期管理能力；discovery、普通 read 或 managed_workspace_admin 不代授配置权限。撤权须在实际披露／写入点生效，不扩展或重定义无关业务 API。

pending retry／discard／resolve／失败记录 SHALL 核 configuration_write 及原 CAS／证明／fencing，返回投影另核 read；retry 状态不得代替真实重启。实际 managed_backend_restart 操作仍核原 managed_workspace_admin，消费配置 candidate 时还须 configuration_write。Gateway restart-development SHALL 保持 local-only／源码环境约束，不能借 Backend 权限新增远端 Gateway 重启；远端 UI 显示需原生管理端处理。

#### Scenario: 远端提交成功但响应丢失
- **WHEN** B 已提交配置而客户端未收到响应，随后本地重连
- **THEN** 查询 B 的原提交身份恢复真实状态，不重复分配候选或将本地镜像回写 B；无管理权限时原生 B 拒绝且没有目标写入

#### Scenario: 只有配置读取授权并在订阅中撤权
- **WHEN** 主体获 configuration_read 而无 configuration_write，读取 B1 来源并订阅事件后尝试提交，随后读取权限也撤销
- **THEN** 提交被原生 owner 拒绝且无写入，撤权后不再披露事件或新读取结果；普通 discovery／read 授权不能绕过该边界

#### Scenario: 配置 retry 不授予实际重启权限
- **WHEN** 主体有配置 read／write、没有 managed_workspace_admin，重试 pending 后请求实际 Backend 或远端 Gateway 开发栈重启
- **THEN** retry 仅按原状态机更新候选，Backend 重启被拒；远端 Gateway 重启仍不可用，配置权限不提升进程管理权限或伪造健康证明

### Requirement: 设置入口必须复用原命令并保持运行面一致

设置及来源详情 SHALL 接入原 Command／Quick Input／Opener 和既有资源寻址规则，自有插件声明的设置按实际 scope／目标提供；需要可重开资源时由统一寻址 owner 登记，不自造 URI 模板。Web 与 Electron SHALL 共用编辑及提交逻辑，原 profile 存储与后端业务事实分别恢复；不能靠清缓存、双写或新原生 IPC 迁移。

#### Scenario: 从命令打开设置并重启桌面产物
- **WHEN** 用户从 `>` 打开 B1 的指定设置，编辑保存后重启打包 Electron
- **THEN** 仍可按稳定目标重开原设置并读取后端真实状态；profile 展示态按原存储恢复，route 变化不改投其它目标，也不依赖开发服务器

#### Scenario: 设置搜索保持目标和来源层
- **WHEN** A/B 同一设置值及修改状态不同，用户在冻结 B 的编辑器用 @modified、@ext、@lang 查询后切焦点到 A
- **THEN** 原过滤交互仅消费 B 的合法 schema、自有插件集合及来源层，保存仍指向 B；不支持语言／扩展维度的业务配置如实说明，迟到 A 结果不覆盖 B 查询

### Requirement: 设置与插件开关必须展示固定范围及真实生效状态

UI SHALL 以包下的插件为开关单位，分别编辑当前 Gateway、当前工作区或新建工作区模板，并在保存前后明确目标、插件、包版本、配置结果及实际运行状态。模板编辑不显示当前实例已启动，重启不重启 Backend／兄弟插件；关闭有代码插件与其贡献使用一个 enabled。停用后原 schema／配置／诊断可读，工具／Skill 新使用不可借设置读取恢复。

#### Scenario: 关闭插件后查看配置
- **WHEN** 用户关闭 A1 的浏览器插件，再编辑它的合法参数并查看新建工作区默认值
- **THEN** 配置保留且按原 writer 保存，不启动浏览器、不改变其他插件或模板；模板有独立归属，关闭和 failed 真实状态不被保存成功覆盖
