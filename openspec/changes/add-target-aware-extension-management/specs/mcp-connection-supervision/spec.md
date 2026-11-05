## ADDED Requirements

### Requirement: 受管制品必须沿原连接启动准入核实

原 MCP SDK stdio 启动准入 SHALL 使用工作区配置冻结的 managed_launch，并在创建真实子进程前由目标安装 owner 核对唯一发布记录、generation、可执行路径及完整 artifact digest。配置解析后的制品变化 MUST 明确拒绝，不以目录查询成功、旧 ready 状态或缓存路径代替实际准入。受管 binding SHALL 保留在原 Server 配置与 connection generation 上，继续沿原 retirement／调用监督结算，不增加另一 MCP client 或调用账本。

安装服务发布新制品 SHALL 不改变已有连接的 launch binding；缺失、篡改或另一 Backend 的制品不得借当前同名安装继续启动。既有显式 command 启动保持原合同。

#### Scenario: 配置解析后制品被篡改
- **WHEN** 配置已解析合法受管引用，但在原 SDK stdio 启动之前 generation 内的依赖内容或可执行文件被改动
- **THEN** 启动前完整摘要核对失败并报告原原因，不创建该 MCP 子进程、不发布 ready 工具目录，也不重写原安装记录为另一个制品

#### Scenario: 目录依赖更新不改变原连接
- **WHEN** 发布的锁定制品已配置且连接 ready，原 PyPI index 随后出现更高版本依赖，真实 Thread 调用该连接工具
- **THEN** 调用仍经原连接与 generation 的绝对 executable，实际依赖保持已锁定版本，不在启动或调用时动态解析 index


### Requirement: 私有 stdio env 必须在原 SDK 准备阶段按定义解引用

私有 env SHALL 复用工作区 MCP 私有输入 owner 与原 stdio SDK 启动链。公开配置只保存绑定 workspace、server、input_key、definition_digest 和不可变 version 的 typed 引用；实际值只保存于该工作区 credentials，并在消费端准备真实连接时解引用，不进入普通配置正文、DTO、事件、模型请求、日志或浏览器持久数据。原明文 env 和 OAuth 继续按各自既有合同运行，不自动迁移，不建立第二 client 或秘密账本。

手动定义 SHALL 按目标侧规范化 command、args、cwd 绑定；目录定义 SHALL 按原 registry 来源、完整 manifest digest 与明确 variant 绑定，写私有值前重读原件并核该 env 已声明为 secret。换来源、命令、工作区、server 或输入键不得借用旧引用。受管安装 generation 不属于秘密来源身份；同源同 manifest 的合法重装可继续使用原引用，但仍独立验证实际 managed launch 的已发布制品。私有值必须非空、可编码为 UTF-8 且不含 NUL，不静默修剪用户输入。

手动 `/private-env` 与目录 `/candidate-plans/private-env` SHALL 共用上述 owner，后端拒绝路由与定义类型不匹配；两者沿原 configuration_read／configuration_write 判权，目录入口另外核 mcp_installation_manage。目录配置保存 SHALL 重新消费原候选请求、核 managed launch 的真实发布来源及实际配置语义，再进入原 source preflight／candidate／CAS writer；不得把脱敏 parameters 投影直接当作 launch 保存。secret 参数模板或 args 尚不能无损表示时明确拒绝，不能拼接为命令行秘密。

#### Scenario: 同名 stdio Server 在两个工作区重启恢复
- **WHEN** A1/A2 经私有写入口为同名 server 设置不同 env 引用，经原配置来源 CAS 保存，并在真实 Backend 重启后调用各自 Thread 工具
- **THEN** 原 SDK 只在各自工作区解引用并使用对应输入；原连接身份和调用归属保持，普通来源、DTO、事件及日志不含秘密值

#### Scenario: 来源或 launch 改变不得借用旧引用
- **WHEN** 调用方将引用用于另一工作区、输入键、registry／manifest／variant，或改变手动 command／args／cwd
- **THEN** 原 parser／SDK 准备明确拒绝，不启动替代目标、不修改原私有版本或通过同名 server 回退

#### Scenario: 目录展示投影不能旁路来源提交
- **WHEN** 用户提交目录 MCP 配置，但原 manifest、计划 digest、已发布制品来源或 source CAS 基线已改变
- **THEN** 原候选与来源 owner 返回明确冲突，不写半份配置、不改绑旧秘密，也不把脱敏展示字段写成实际 env
