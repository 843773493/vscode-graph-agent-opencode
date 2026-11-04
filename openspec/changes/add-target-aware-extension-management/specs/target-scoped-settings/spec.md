## Purpose

定义同一工作台内面向当前 profile、本地及远端 Gateway、目标环境 Workspace 用户来源层和指定工作区的统一设置编辑体验，保留各域原有权威存储、合并优先级、提交并发控制和生效状态，避免跨目标配置误写或形成第二事实源。

## ADDED Requirements

### Requirement: 设置编辑必须持续展示并冻结明确目标

设置入口 SHALL 使用原设置编辑器及目标选择，区分 Workbench profile、原生 Gateway、目标环境的 Workspace 用户来源层、精确 workspace 及可用 runtime override。页面须持续展示目标、来源层和可写能力；切换编辑器或导航不改投未提交内容。默认值只读，只能在合法覆盖层修改；未知、不可访问或不合法的目标须拒绝。

#### Scenario: 编辑远端设置期间切换会话
- **WHEN** 用户在 B1 的 workspace 来源层编辑 MCP 参数，随后切到 A1 会话并保存
- **THEN** 保存仍指向 B1 原来源层，界面持续显示 B1；B1 失效时明确失败，不写 A1 或用户默认层

### Requirement: 设置归属必须复用原 owner 而非统一成新配置域

Workbench 展示设置、Gateway runtime 配置、Workspace 配置、会话设置、Gateway 用户视图和 registry 手动操作 SHALL 保留各自权威来源及提交语义。统一设置 UI 只导航／编辑合法对象，MUST NOT 将非配置状态伪装成 runtime candidate，或增加跨 Gateway 自动继承／同步规则。业务配置更改不得只写前端状态。

#### Scenario: 布局与工作区默认 Agent 分别修改
- **WHEN** 用户修改当前 profile 的布局，再修改 B1 的 default_agent
- **THEN** 两者分别写原展示态和 B1 配置 owner，不互相产生对方的配置提交；既有会话继续遵守原 Agent 绑定规则

### Requirement: 扩展设置必须保留原 Code OSS 配置域及视图语义

VSIX 贡献设置 SHALL 进入原 configuration registry，保留 setting scope、默认值、policy、信任限制和语言覆盖，不写入 Gateway／Workspace 业务 JSONC。原 get／inspect／update／change 事件 SHALL 使用 host view 配置模型；Global 依原 USER 与 scope 定位 profile local 或目标 remote user，Workspace 定位稳定 Code OSS view 配置，WorkspaceFolder 按显式 resource 定位挂接根的 `.vscode/settings.json`。单根沿原 folder 模型，multi-root 须有原服务持有的稳定 `.code-workspace` 配置资源；默认资源可物化在目标基础设施 profile 区，不能把 Workspace 写分摊成多个业务配置提交。

原配置初始化及事件 SHALL 投影到所属目标／view，inspect 与设置 UI 对相同 resource／language 一致。省略参数沿原推导规则；无唯一 owner、资源不属 view 或不可写时明确拒绝，不跟随窗口焦点改投。窗口级 schema 的版本冲突须在启用阶段明确处理。

系统 SHALL 在原配置服务边界按 host／view 构造或路由原 WorkspaceConfiguration／配置模型，共用 registry／scope／JSON editing 和已登记目标 FileService，不能轮换单窗口全局 workspace 或在 renderer 猜目标本地路径。

#### Scenario: 多根扩展设置与业务配置分开写入
- **WHEN** A view 挂接 A1/A2，扩展对同一键执行 resource／language 读取与 inspect，并分别更新 Global、Workspace、WorkspaceFolder，焦点随后切到 B1
- **THEN** 更新分别进入原 profile／remote user、稳定 A view 及指定 A folder 配置，读取与事件仅反映合法视图；A1/A2 的 `.boxteam/workspace.jsonc` 和 B 配置不被改写，非法资源请求明确拒绝

### Requirement: 来源编辑必须补齐原配置 owner 的真实读写合同

现有 sources 元数据或有限 runtime_override PATCH MUST NOT 被当作完整来源编辑能力。系统 SHALL 在原 Gateway／Workspace 配置 owner 增补合法可编辑层发现、脱敏来源正文、schema／effective／遮蔽信息读取、来源预检和 CAS writer，并迁齐正式 owner delta、协议及消费者。正文读取的脱敏占位不得被当作密码原值写回；没有 writer 的层 SHALL 明确只读，不经通用文件 API 或任意 JSON 路径写入绕过 owner。

原单 workspace 更新的 SQLite 物化合同 SHALL 保持不变；显式来源文件编辑经该 source writer 的文件 CAS／原子写入和原导入／fan-out，不由 runtime 更新顺带回写 JSONC。来源保存、层物化和 active 生效 SHALL 分别报告，不形成双写事实源。能力未完成时只读是阶段诊断，不满足本稿所列合法来源编辑的最终验收。

#### Scenario: 从来源摘要打开完整设置编辑
- **WHEN** 用户打开 B 的 Gateway user JSONC 和 B1 的 workspace JSONC，提交一个现有 runtime PATCH 不支持的合法字段
- **THEN** 通过各自正式 source 读写合同完成 schema／CAS／候选闭环，或明确说明该层尚不可编辑；不得把来源摘要当正文、放宽原有限 PATCH 或旁路改文件后伪报成功

### Requirement: 有效设置必须揭示完整来源及遮蔽关系

项目业务设置 SHALL 展示字段所属域、source layer、原来源资源、revision／digest、最终有效值、遮蔽关系、active／pending 和实际生效分类；Code OSS 设置沿原 inspect 模型，不伪造业务候选字段。合并顺序、对象合并及数组替换 SHALL 沿各自原配置合同，不由前端另算一套有效值。API 只物化来源层而未回写文件时必须如实表达；秘密值不得包含在普通诊断中。

#### Scenario: 用户默认值被工作区覆盖
- **WHEN** 同字段在 inline、user、user_local 与 B1 workspace 层分别存在
- **THEN** 显示每层来源及 B1 最终有效层，修改被遮蔽的 user 值不伪报 B1 active 已改变；不丢失 user_local 的独立 revision

### Requirement: 表单与 JSONC 必须共用原候选提交及并发合同

项目业务配置的表单和 JSONC 编辑 SHALL 经相同 schema／预检、来源层和 active 基线、幂等提交及候选流程，不使用通用文件保存绕过配置 owner。Code OSS 设置 SHALL 使用原 configuration editing owner，不转换为业务候选。成功须以相应 owner 完整结果替换镜像，冲突／失败须重新读取并保留可处理的编辑内容，不能自动覆盖并发更改。删除覆盖只移除所选层键；格式及注释沿原 writer 处理。

#### Scenario: 表单与 JSONC 并发修改同层
- **WHEN** 两个编辑器读取同一基线，一个先提交成功，另一个以旧基线保存
- **THEN** 后者收到明确冲突及最新来源，不静默覆盖；删除覆盖时显示重新暴露的有效值，而非把默认值写入该层

### Requirement: 配置保存与运行生效必须分别展示

项目业务设置 UI SHALL 显示原 owner 的候选／提交身份、已应用及延后路径、active／pending、错误与重启要求；Code OSS 设置及扩展使用其原有 reload／应用状态，不伪造统一候选。仅配置保存或 HTTP 成功 MUST NOT 作为资源已生效证明。重启、重试或丢弃 pending SHALL 使用原管理生命周期和基线，不重放已执行副作用；未登记字段不得被 UI 自行归类为可热应用。

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

设置及来源详情 SHALL 接入原 Command／Quick Input／Opener 和既有资源寻址规则，扩展贡献的设置按实际 scope／目标提供；需要可重开资源时由统一寻址 owner 登记，不自造 URI 模板。Web 与 Electron SHALL 共用编辑及提交逻辑，原 profile 存储与后端业务事实分别恢复；不能靠清缓存、双写或新原生 IPC 迁移。

#### Scenario: 从命令打开设置并重启桌面产物
- **WHEN** 用户从 `>` 打开 B1 的指定设置，编辑保存后重启打包 Electron
- **THEN** 仍可按稳定目标重开原设置并读取后端真实状态；profile 展示态按原存储恢复，route 变化不改投其它目标，也不依赖开发服务器

#### Scenario: 设置搜索保持目标和来源层
- **WHEN** A/B 同一设置值及修改状态不同，用户在冻结 B 的编辑器用 @modified、@ext、@lang 查询后切焦点到 A
- **THEN** 原过滤器仅消费 B 的合法 schema、扩展集合及来源层，保存仍指向 B；不支持语言／扩展维度的业务配置如实说明，迟到 A 结果不覆盖 B 查询
