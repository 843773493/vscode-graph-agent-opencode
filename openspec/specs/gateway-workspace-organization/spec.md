# gateway-workspace-organization Specification

## Purpose
TBD - created by archiving change add-scalable-session-resource-generators. Update Purpose after archive.
## Requirements
### Requirement: Gateway 持久化工作区文件夹树
Gateway SHALL 使用稳定节点 ID 持久化工作区文件夹和工作区引用，并 MUST 在重启后恢复名称、父节点和排序。

#### Scenario: 重启恢复组织结构
- **WHEN** 用户创建嵌套工作区文件夹、移动工作区并重启 Gateway
- **THEN** Gateway 返回与重启前相同的树、稳定节点 ID 和顺序

### Requirement: 工作区树拒绝非法关系
Gateway MUST 拒绝未知工作区、重复规范引用、自身父节点和循环父子关系，并返回包含目标 ID 的明确错误。

#### Scenario: 移动形成循环
- **WHEN** 用户把父文件夹移动到其子文件夹下
- **THEN** Gateway 拒绝更新且原树保持不变

### Requirement: 工作区 breadcrumb 可定位
Gateway SHALL 为工作区文件夹和工作区引用返回从根到目标的稳定 breadcrumb。

#### Scenario: 搜索定位嵌套工作区
- **WHEN** 搜索命中嵌套文件夹中的工作区
- **THEN** 响应包含每一级节点 ID、名称和目标 workspace ID

### Requirement: 工作区文件夹是可嵌套的虚拟组织树
Gateway SHALL 允许工作区文件夹任意多层嵌套，并 SHALL 只保存文件夹和工作区引用的虚拟父子关系；它 MUST NOT 将工作区文件夹映射为或写入任何工作区物理目录。

#### Scenario: 拖放形成多层工作区文件夹
- **WHEN** 用户把工作区文件夹拖到另一工作区文件夹，或把工作区引用拖到任意层级的工作区文件夹
- **THEN** Gateway 原子更新虚拟父节点和排序，重启后恢复相同层级，工作区本地目录不发生移动

#### Scenario: 拖放形成子工作区
- **WHEN** 用户把工作区拖到另一工作区
- **THEN** Gateway 更新来源工作区的 `parent_workspace_id`，拒绝自身或后代作为父工作区，并在资源树中把来源工作区显示为目标的子工作区

#### Scenario: 子工作区拖入虚拟文件夹
- **WHEN** 用户把已有父工作区的工作区拖到虚拟工作区文件夹或导航根
- **THEN** Gateway 清空其 `parent_workspace_id` 并更新虚拟导航引用，工作区物理根目录不发生移动

#### Scenario: 工作区文件夹使用右键菜单管理
- **WHEN** 用户新建、重命名或删除工作区文件夹
- **THEN** Web 通过根标题或节点右键菜单和树内编辑态调用 Gateway API，根标题不伪装成创建按钮，也不显示要求手工输入内部 ID 的弹窗

#### Scenario: 删除非空虚拟工作区文件夹
- **WHEN** 用户确认删除包含子文件夹或工作区引用的工作区文件夹
- **THEN** Gateway 原子删除该虚拟文件夹子树，把其中工作区引用提升到被删文件夹的父级，并且不删除或移动任何真实工作区

#### Scenario: 工作区文件夹拖回导航根
- **WHEN** 用户把嵌套工作区文件夹拖到“工作区文件夹”根入口
- **THEN** Web 使用明确可命中的根级拖放目标把虚拟父节点清空，并保持工作区物理目录不变

