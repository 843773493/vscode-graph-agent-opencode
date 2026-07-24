## ADDED Requirements

### Requirement: 联邦工作区可参与本地目录与生成器配置
本地 Gateway SHALL 允许工作区文件夹、目录搜索和 GeneratorDefinition 引用其直接导入的远程工作区投影，并 MUST 使用本地稳定 workspace ID 路由到远程 Gateway。

#### Scenario: 生成器挂载远程会话
- **WHEN** 用户把生成器挂载到直接导入的远程工作区会话
- **THEN** 本地 Gateway 通过远程 Gateway 代理校验和执行，并在远程离线时保留配置且标记 blocked/offline

### Requirement: 禁止嵌套联邦生成路由
本地 Gateway MUST NOT 通过远程 Gateway 继续引用其导入的嵌套远程工作区作为生成或目录目标。

#### Scenario: 远程目录快照包含嵌套工作区
- **WHEN** 远程 Gateway 返回非其直接拥有的工作区目录能力
- **THEN** 本地 Gateway 排除该目标并报告有界联邦错误
