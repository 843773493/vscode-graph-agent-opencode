## ADDED Requirements

### Requirement: 自有插件配置与业务资源必须复用现行寻址 owner

插件配置 SHALL 使用其 Gateway／Workspace 原配置来源资源，继续遵守现行 config kind、逻辑来源名、独立 layer 及 URI／身份分离合同；无稳定可寻址来源文件时保持无 URI。Workbench 展示态仍使用原 profile，不为插件模拟 vscode settings／多根 view 或扩展宿主文件。

本 change SHALL 撤销尚未发布的用户 VSIX `extensions` 包根及 `config/extension-host/...` 模板提案，不将其当作新格式的兼容地址。旧接入的构造器、解析／映射和消费者 SHALL 迁齐后删除；主规范的 kind 闭集和既有资源不因本次规划改写而被隐式放宽或删改。插件包按安装记录身份管理，instance_id、端口、命令、进度不普遍造 URI。

终端／浏览器／MCP 的既有可引用业务资源 SHALL 维持原规范 URI、独立版本及 owner；Gateway 代理只改变传输路径，不按服务位置重命名资源。确需新增可重开资源时 SHALL 先由唯一寻址 owner 正式登记模板／能力并同步消费者，不在 UI 或插件自行拼接 grammar 或提供任意原生路径旁路。

自有贡献的 command_id、registration_id 和通道代际 SHALL 只用于注册／调用／失效，不自动成为资源 URI。未来开放文件系统、文本内容或编辑器 Provider 时，稳定可重开对象仍须按原寻址合同登记；标准工作台 URI 投影不转移业务身份与版本的权威来源。

#### Scenario: 插件设置复用工作区来源资源
- **WHEN** 用户从快速访问重开 A1 插件设置，随后 Gateway 路由或服务 instance_id 改变
- **THEN** 仍解析原 A1 来源并经配置 owner 读取／提交，地址不包含临时端口或进程身份，不重建宿主 settings 文件

#### Scenario: 服务统一托管不改变终端与浏览器资源身份
- **WHEN** 原终端／浏览器服务迁为受管插件或 Backend 重新连接
- **THEN** 原业务 URI 和资源 ID 继续指向同一 owner，失效资源明确报错，不把新实例资源伪装成旧 URI 的成功恢复

#### Scenario: 旧扩展地址不能启动兼容链路
- **WHEN** 调用方使用已撤销的用户 VSIX 包根或 extension-host 配置地址
- **THEN** 明确拒绝不支持的模板，不启动旧宿主、不改投自有包或工作区普通文件路径

### Requirement: 插件 Skill 及配套文件必须复用技能资源寻址

plugins[].contributes.skills 的 SKILL.md、相对引用文档／示例和目录导航 SHALL 接入现有 Skill／文件资源 owner，由其正式登记合法展示 URI 和读取／列举／搜索能力；复用原 kind 闭集，不恢复 VSIX extensions 包根、不由插件或 UI 拼接 grammar。Skill 的来源绑定 SHALL 保留原生 Gateway、启用范围、包版本／摘要、plugin_id 和贡献身份；内容 revision 与服务 instance_id 分离，服务重启不重命名技能资源。逻辑来源切换和历史冻结读取 SHALL 由原来源及上下文 owner 按实际版本解析，不能仅按同名新包重定向。

读取相对配套文件 SHALL 限于原贡献合法目录和精确目标，沿原认证资源链解析，不暴露物理安装路径或复制到工作区。读取／列举／搜索不触发 skill_load、不执行包脚本，也不自动跟踪；未登记能力或失效来源明确拒绝。

#### Scenario: 远端 Skill 配套文件与实例重启
- **WHEN** A 经 Relay 打开 B 的 Skill 并读取其相对示例，期间 B 服务重启
- **THEN** 示例仍由 B 原资源 owner 按原贡献和版本读取，展示 URI 不包含端口／实例或 A 本地路径；读取不激活 Skill，非法越目录引用明确拒绝
