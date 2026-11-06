## Why

同一工作台需要管理当前 Gateway 及远端 Gateway 下、一个后端承载的多个工作区。仅展示原版扩展／MCP 面板无法确定安装位置、执行宿主、工作区启用范围与配置归属，也不能让原版 MCP 运行时自动接入项目 Agent。

需要复用 Code OSS 的成熟界面与宿主机制，同时使安装、执行和设置始终指向明确目标，复用现有工作区 MCP owner、配置提交及联邦管理链路。

## What Changes

- 复用原版扩展视图、MCP 列表／详情／搜索／安装流程，以及设置编辑器、命令和 Quick Input；按“此工作台／运行目标／工作区”展示归属。
- 分开 VSIX 扩展市场与 MCP 服务目录，使用可配置且经真实验证的来源；本地 VSIX、手动 MCP 定义与目录安装进入各自同一管理链，不承诺所有第三方市场或扩展均可用。
- 固定安装目标、配置范围和运行实例三种不同身份：目标机器可复用安装包，工作区独立启用、参数、凭据与 MCP 连接；共享包不意味着共享进程或授权。
- 由目标后端侧的安装／宿主服务持有实际安装和运行事实；原生目标 Gateway 最终判定管理权限，Relay 只转送。Gateway 不执行插件代码、不持有工作区 MCP 工具目录、不直接读写工作区业务数据。
- MCP 的新安装界面消费现有配置 owner、`McpCatalogOwner`、连接操作、OAuth 与调用监督；继续使用唯一目录／generation／租约，不再启动一套与项目 Agent 无关的 MCP 运行时。
- 纯 Web 扩展使用当前 Workbench 的 Web Extension Host；需要 Node 的扩展在明确本地／远端后端目标上的 Node Extension Host 执行。Electron 仍只承担窗口、生命周期和固定 profile，复用同一 renderer，不增加原生文件系统 IPC 或 Electron 插件宿主。
- 将原版单 Remote running location 扩展为有明确目标及工作区视图的宿主路由，复用原扩展 API／RPC／贡献机制；明确同名命令、资源 Provider、终端、调试、Webview 和存储的归属，不靠切换全局 remoteAuthority 支持多目标。
- 迁齐按扩展 ID 唯一索引的内部注册与激活链，支持同 ID 多目标实例；文档、编辑器、配置事件和运行态贡献按宿主视图投影，窗口级声明冲突明确诊断。
- 复用现有配置来源层、schema、CAS、幂等提交、active／pending、用户级 source fan-out 和 restart 分类，提供统一目标选择、来源说明、表单与 JSONC 编辑。远端保存自己的配置，本地只保存连接登记与展示镜像。
- 明确补齐现有配置 API 尚缺的来源正文／schema／source writer，以及远端配置读写能力；VSIX 设置独立沿原 Code OSS profile／remote user／workspace／folder／语言覆盖模型，不混入 Python 业务配置。
- 新增目标侧实际 MCP 制品安装与普通敏感输入的私有引用链；保留动态 MCP definition Provider 的发现／resolve／变更／撤销生命周期，制品引用沿原连接代际监督结算。
- 扩展／MCP 命令与管理对象接入 `add-workbench-quick-access` 的原 Registry、Command、Opener 和查询合同；注册能力后由标准入口消费，不另建项目插件 ABI、全局索引或设置事实源。
- 明确兼容能力矩阵、安装／配置／运行的不同阶段、断线及重启恢复、同机多工作区隔离和真实 Web／Electron／远端验收。任务按职责组织，不定义全局串行实施顺序。

## Capabilities

### New Capabilities

- `mcp-installation-management`: 目标感知的 MCP 目录发现、安装、版本、工作区启用、共享包引用与既有 Agent MCP owner 集成。
- `multi-target-extension-hosts`: 原版扩展管理与 Web／多目标 Node 宿主、贡献路由、能力约束、存储隔离及生命周期。
- `target-scoped-settings`: 同一工作台内按 Workbench、原生 Gateway、Workspace 来源层编辑设置，并展示真实来源、生效与冲突状态。

### Modified Capabilities

- `configuration-domains`: 补明确选中来源文件的读取、无副作用预检与文件 CAS writer，保留原单工作区 SQLite 层物化及用户 source fan-out 语义。
- `configuration-evolution`: 区分来源保存、层物化与 active／pending 生效，沿原候选、幂等提交及配置事件报告真实状态。
- `remote-gateway-federation`: 登记默认拒绝且绑定认证主体的 configuration_read／configuration_write，覆盖实际披露与写入点，并与原生命周期权限分开。
- `virtual-resource-addressing`: 为目标原 Code OSS profile settings 和稳定多根 workspace 文件登记两个具名 `config` URI 模板、精确文件／直接父级能力及认证宿主映射，不改变既有业务 ConfigSource 的单段逻辑名。

现行 `mcp-connection-supervision`、`extension-connection-management` 和多工作区挂载的原业务状态机保持不变；私有输入引用、受管 launch binding 及安装／宿主管理合同仍须随对应实施切片按 task 1.6 补齐原 owner delta／schema／消费者。涉及原合同的其它变化须先明确规划，不借 UI 接入默改行为。

## Impact

- Workbench：现有贡献及 sourceTransform、原扩展／MCP 管理服务、扩展宿主路由、设置编辑器和快速访问接入；不修改固定上游子模块或复制原版面板。
- 后端：工作区 MCP 与配置原 owner，以及明确后端目标侧的包安装和 Node 扩展宿主基础设施；这些进程不属于 Electron main，也不属于 Gateway 的 Agent 业务服务。
- Gateway：复用目标 registry、联邦管理授权、路由、生命周期和请求追踪，暴露原生目标的管理能力；远端 Relay 不复制安装／配置事实。
- 协议与发行：增加明确目标、能力、安装操作及宿主通道合同，迁齐生成物及消费者。运行时组件、离线资源与版本需进入真实发行产物验证；不依赖开发服务器或源码目录。
- 规划组织：一个独立 change 内拆分三个 capability，与快速访问变更双向关联；本轮只写规划，新增任务全部未完成，不授权安装包、配置迁移、服务切换或产品实施。
