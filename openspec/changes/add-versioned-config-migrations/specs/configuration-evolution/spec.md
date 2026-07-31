## ADDED Requirements

### Requirement: 配置文件独立版本迁移
系统 SHALL 为用户级与工作区级配置分别维护 `config_version`，并在合并配置前按各自版本顺序迁移到当前版本。

#### Scenario: 用户配置和工作区覆盖版本不同
- **WHEN** 用户配置与工作区配置分别处于不同旧版本
- **THEN** 系统分别迁移并原子写回两个文件，再按既有优先级合并当前版本配置

#### Scenario: 重复执行迁移
- **WHEN** 当前版本配置再次执行迁移
- **THEN** 文件内容保持不变且命令报告无需迁移

### Requirement: 内置工具使用稳定标识
持久化配置中的内置工具 SHALL 使用稳定 `tool_id`，不得依赖 Python 模块或工厂函数路径；自定义扩展 MAY 继续使用显式 factory。

#### Scenario: 内置工具源码重构
- **WHEN** 内置工具实现移动到新的模块或函数
- **THEN** 未变化的 `tool_id` 配置无需迁移即可解析到新实现

### Requirement: 迁移旧会话历史工具
迁移器 SHALL 将旧的会话历史读取、grep 和 JSONL 工具声明替换为 `read_context` 与 `search_context`，且不得修改会话目录或历史事件。

#### Scenario: 旧工作区配置包含三个历史工具
- **WHEN** 工作区配置包含已删除的三个 session history factory
- **THEN** 迁移结果只包含 `read_context` 与 `search_context`，旧会话可使用当前配置重试

### Requirement: 配置提交前预检工具
启动和热重载 SHALL 在提交候选快照前检查内置工具 ID、自定义 factory 导入、工具名唯一性和声明结构；无效候选 MUST 报告来源文件和配置路径且不得进入运行时。

#### Scenario: 自定义工厂函数不存在
- **WHEN** 候选配置引用模块中不存在的 factory 属性
- **THEN** 配置加载立即失败并指出配置来源与属性路径，而不是在发送消息后失败

### Requirement: 提供迁移与诊断命令
配置 CLI SHALL 支持迁移用户配置、指定工作区配置和诊断配置，不得通过强制重建覆盖用户自定义值。

#### Scenario: 诊断旧配置
- **WHEN** 用户运行配置诊断命令
- **THEN** 命令报告当前版本、待迁移步骤、工具解析结果和具体错误位置，不修改文件

