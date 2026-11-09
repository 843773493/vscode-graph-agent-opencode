## Why

当前 Workbench 已复用 Code OSS 的注册机制并接入多项业务能力，但自有插件尚不能统一声明贡献或注册 Provider；浏览器、终端与 MCP 也具有不同的托管边界。本变更借鉴 VS Code 的完整扩展架构，建立项目自有的声明、注册、调用与释放合同，首批只开放少量能力，运行进程统一由目标 Gateway 托管。

## What Changes

- **BREAKING**：取消第三方 VS Code 插件兼容，删除项目接入的 VSIX 安装／扫描／卸载、Marketplace／Gallery、Web／Node Extension Host、扩展 API／RPC、动态 VSIX MCP Provider 及相关配置、构建、协议和测试链路。不得保留转换器、旧格式 fallback 或第二安装／运行体系。
- 统一浏览器管理器、终端管理器、源码调试、MCP 以及需要创建进程的安装阶段为受管服务插件。Backend 始终通过 HTTP／流式通道访问 Gateway，再由目标原生 Gateway 路由到精确插件实例；不保存插件监听端口，不直接创建这些执行子进程。`python_exec`、持续任务条件脚本和视频预处理 ffmpeg 明确排除本次迁移，保留原调用方式、Backend 执行位置和现有进程管理，不接入 Gateway 或终端插件。
- 自有包使用标准 `.zip` 归档，根目录包含 `plugin.jsonc`，不规定专用文件后缀；manifest、扩展合同与管理协议分别版本化。区分声明式贡献、运行期 Provider 与受管服务；纯声明插件无需运行入口，有运行代码时支持 Python／Node，不沿用 VSIX manifest 或 vscode API。
- 统一“插件包 Plugin Package／插件 Plugin／贡献 Contribution／运行实例 Runtime Instance”。ZIP 是整体安装更新单位，plugins 是独立启停单位，贡献全部归插件；不保留根级运行入口或另一套模块／功能管理实体。同包插件独立实例，纯声明插件无进程。
- 首批开放命令、菜单、快捷键、配置、受管服务、源码调试与 Skill 七类贡献，直接接入已有 Workbench Registry／业务服务；建立扩展点版本和支持状态、显式插件目标、开关驱动启停、取消／事件与注销释放合同。调试迁移已有 Node Inspector JavaScript 业务及20个工具，经标准 DAP 接原 Debug UI；不依赖 js-debug VSIX，不宣称已支持 Python 调试。搜索、树、编辑器、文件系统、语言、通用 Terminal／Task Provider 等仍未开放，不因上游有实现而伪报插件可用，不预写空 Provider。
- UI 复用原插件管理视图，按“插件包 → 插件 → 运行实例”展示，支持当前 Gateway、当前工作区的开启／关闭／重启及新建工作区默认值；默认值只在创建时冻结初始化，不修改已有工作区。插件开启随所属范围运行，工具、命令和 Skill 不触发启动；包级批量开关逐项报告，不增加包级 enabled。
- Gateway 复用原进程管理体系，支持每 Gateway 唯一实例和每工作区独立实例；实际 PTY、浏览器、stdio 子进程由所属服务持有。共享包、共享进程和共享业务资源分别表达，工作区状态和秘密不迁入 Gateway。
- 使用一套简单默认生命周期：明确启动／停止、独立连接状态、崩溃报错、显式重启、新运行身份、写操作结果未知不重放。Backend 重启、页面关闭及 Tab 切换不停止插件；高级恢复和按服务定制生命周期以后按实际需求增加。
- 通用插件秘密参数纳入本轮：借鉴 VS Code 将普通配置与秘密存储分开的做法，支持密码输入、保存后复用、更换和清除，也保留现有环境变量引用。由所属 Gateway／Workspace 配置 owner 沿现有受保护私有文件机制保存不可变值，普通来源仅保存绑定目标、插件与参数位置的引用；writeOnly 驱动秘密输入与禁止回显，允许经专用受保护写入口提交真实值，禁止明文进入普通配置。实际值只在认证运行交付中传给对应插件，不进入 Gateway 工作区接管记录；不新增通用 SecretStorage SDK、秘密账本或系统密钥链框架。
- 沿 Gateway 原配置及日志热更新机制提供统一诊断、可开关的本地遥测与统计；关闭采集时停止对应开销，普通业务请求不附带安装审计、进程扫描或全量健康检查。
- 原扩展管理 UI 的搜索、列表、详情、安装／更新／卸载、设置及标题目标选择直接消费自有插件模型。保留各视图目标和草稿归属，不伪造 VSIX 对象以继续调用旧服务。
- 分发只预留项目自有公网目录／下载协议与可配置来源；当前无公网服务，未配置时明确显示不可用并且零外部查询。支持自有格式的本地包及显式开发目录；不回退到 VS Code、Open VSX 或 MCP 第三方目录。
- MCP 继续使用标准 Streamable HTTP，stdio 服务在插件内托管；终端与浏览器的 HTTP／WebSocket／SSE 同样经过 Gateway，现有业务操作、资源 ID、输出顺序及结果归属保持。外部原生 HTTP MCP 通过 Gateway 路由，不强制创建本地包装进程。
- 安装、插件运行、工作区插件启用及配置提交分别报告真实状态。复用唯一安装记录、工作区 MCP owner、配置来源／CAS／active／pending、联邦授权和 request_id；Agent 的会话、调用租约及历史由 Backend 原 owner 维护。
- 版本切换采用固定流程：预校验、原来源 CAS 提交期望、撤准入、停旧核退出／启新、原 owner 顺序发布；预校验失败保留原状态，替换阶段失败停止推进并报告真实结果，不自动回滚、补偿或循环重试。MCP 仅沿 Backend 原 journal／claim／publication 协调，Gateway 不建第二候选状态机；UI 消费完整快照派生投影。卸载只核原实例、active／pending 绑定、模板／创建 intent 和读取／使用 lease，未知引用拒绝本次卸载，不建立全局巡检。未绑定参数限原配置登记容器保留且未生效，重绑定严格验证。人工工具确认仅回归原显式可选机制，普通会话及插件管理不新增审批入口、等待状态、轮询或执行恢复。
- 按实际 default／agent-collaboration／debugging 注册组清点工具族，保留公开工具名、输入输出、调用资格和结果封存合同。Agent 暂停／断点／快照、Goal／任务调度、协作、文件／搜索、技能及 HTTP 查询继续由原业务 owner 负责；只迁移本次明确纳入的执行链，不把所有工具强制改为 MCP，不误删名为“扩展工具”的业务注册。
- 插件通过 plugins[].contributes.skills 携带 SKILL.md、配套文档／示例和明确工具合同关联；复用原 Skill 来源、SkillCatalog、URI、skill_load 与上下文版本机制，不建立第二技能目录或逐工作区复制包。安装、插件开关、Skill 加载与工具准入分开，代码启动仅由插件开关／所属运行时恢复驱动；保留原调用信封及 tools.custom[].skills 关联，allowed-tools 不作为授权。浏览器／调试 Skill 随对应能力纵向迁移，Web 搜索／Gateway 上下文 Skill 保持原 owner；更新、撤权和卸载明确处理冻结版本及引用，不自动注入说明或执行包内脚本。
- 快速访问消费自有插件的正式命令、状态及资源入口；Web 与 Electron 构建同一工作台，Electron 不承担插件进程或新增原生 IPC。
- 明确插件包推荐布局、产品源码落点和运行数据归属：根清单显式声明各插件入口／贡献路径，内部目录不作为发现协议；扩展点合同目录集中维护，注册仍接原服务。Gateway 插件管理复用原 runtime，已安装包只读，实例日志及工作区业务数据沿原 owner 存储，自带包与下载包采用同一格式。

## Capabilities

### New Capabilities

- `multi-target-extension-hosts`：保留本 change 的能力目录标识，覆盖自有插件的声明／运行期注册／受管服务三层架构、首批能力、目标与生命周期、遥测及发行合同；不表示继续支持 VS Code Extension Host。
- `mcp-installation-management`：自有插件来源及安装在 MCP 服务上的使用、工作区绑定与标准 MCP 接入，取消第三方目录／VSIX Provider 和 Backend 直接启动链路。
- `target-scoped-settings`：复用设置 UI 编辑自有插件及既有 Gateway／Workspace 配置，保留明确目标、schema、来源、CAS、秘密引用和真实生效状态。

### Modified Capabilities

- `configuration-domains`：保留来源编辑与 fan-out；明确 Gateway 的插件登记／诊断配置与 Workspace 服务参数的边界，移除 Backend managed stdio launch 配置。
- `configuration-evolution`：保留来源保存、物化及 active／pending；新增受管实例热应用实际 revision 的可观察结果。
- `mcp-connection-supervision`：进程托管归 Gateway／插件，工作区 MCP 连接、目录和执行资格仍归 Backend 原 owner，取消 Backend stdio 创建路径。
- `remote-gateway-federation`：原生目标最终判权，安装／运行管理独立授权；统一 Backend、Relay 与流式插件路由，不扩大普通业务权限。
- `virtual-resource-addressing`：撤销本 change 尚未发布的 extension-host profile／view 地址提案，设置继续使用既有配置资源合同；插件 Skill 及配套文件接原技能资源 owner，不恢复旧 VSIX 包根或自造 URI；不为进程或临时端口造业务 URI。

## Impact

- Gateway：复用 registry、进程句柄／组、接管、日志、联邦和配置设施；新增具名插件管理及业务代理，不运行 Agent 业务、不直接读取工作区 `.boxteam/`。
- Backend／workspace-services：Terminal／Browser 客户端、源码调试和 MCP 会话统一访问 Gateway，服务内部保留原资源 owner；安装子进程按原规划迁入受管执行边界。Python 工具、条件脚本和 ffmpeg 保留现有实现；存储一致性内部原语和宿主系统打开操作同样不在本次迁移范围。
- Workbench／发行：复用原注册表与 UI 服务承接自有贡献，不创建第二套命令／菜单／设置系统；迁齐旧体系的有效业务消费者后删除旧市场、用户扩展宿主和打包接线，保留原版编辑器与内置资源，不修改 reference_repo 或固定上游源码。
- 本轮只修订 OpenSpec，不实施代码或修改用户环境。已有任务与验证原文在 tasks.md 历史区保留，不计作新架构已实现；后续迁移按纵向链和真实 release 基线执行。
