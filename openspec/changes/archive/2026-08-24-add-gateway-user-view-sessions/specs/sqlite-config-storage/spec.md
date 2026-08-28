## Purpose

将 Gateway 与 Workspace 的共享可变配置、控制状态和用户运行时状态放入职责清晰的 SQLite 存储，同时保留发行默认配置、schema、目录权威索引和单会话 rollout 索引的既有边界。

## ADDED Requirements

### Requirement: Gateway 共享状态持久化

Gateway SHALL 将共享可变功能配置、Gateway 工作区注册状态、用户身份元数据、访问租约和用户视图状态保存到 Gateway 控制面 SQLite；这些状态必须支持事务性更新。

#### Scenario: 并发申请用户租约

- **WHEN** 两个客户端同时申请同一个普通用户
- **THEN** SQLite 事务只允许一个申请成功，另一个得到明确的占用冲突结果

#### Scenario: 接管更新租约

- **WHEN** 接管操作更新旧租约和新租约
- **THEN** 旧租约失效与新租约创建作为一个原子状态变化对后续请求可见

### Requirement: Workspace 共享状态持久化

工作区后端 SHALL 将工作区共享可变功能配置和工作区级会话活动状态保存到工作区 `.boxteam` 范围内的 SQLite；Gateway 不得直接读写这些工作区业务数据。

#### Scenario: 不同用户读取工作区配置

- **WHEN** 两个普通用户通过同一 Gateway 读取同一工作区功能配置
- **THEN** 两个用户得到相同的工作区有效配置，且配置不因用户视图切换而改变

#### Scenario: 工作区后端重启后读取状态

- **WHEN** 工作区后端重启后重新提供服务
- **THEN** 工作区共享配置和已持久化的活动状态可以从工作区 SQLite 恢复

### Requirement: 配置默认值和状态数据库边界

系统 SHALL 保留发行包 JSONC 默认配置和 schema 的职责；系统不得将工作区目录权威 `session-catalog-index.json` 或单会话 rollout `index.sqlite` 改造成 Gateway 全局配置存储。

#### Scenario: 合并有效配置

- **WHEN** Gateway 或 Workspace 启动并加载共享功能配置
- **THEN** 系统使用发行默认值、有效的共享持久化覆盖和 schema 生成并校验有效配置，校验失败必须明确报错

#### Scenario: 读取单会话 checkpoint

- **WHEN** 工作区后端加载一个会话的 checkpoint 或 Turn 历史
- **THEN** 系统继续使用该会话 rollout 下的 `index.sqlite` 和 JSONL 内容，不读取 Gateway 用户数据库代替会话索引

### Requirement: 可恢复配置迁移

从现有 JSON 状态迁移到 SQLite 或用户 profile 时，系统 SHALL 使用可恢复写入、显式版本和失败报告；迁移失败不得删除原始配置或返回部分有效配置。

#### Scenario: 成功迁移

- **WHEN** 现有 Gateway 或 Workspace JSON 状态通过迁移流程导入
- **THEN** 系统写入版本化 SQLite 状态，重新校验有效配置，并保留可核验的迁移结果

#### Scenario: 迁移失败

- **WHEN** JSON 状态格式错误、schema 校验失败或 SQLite 写入失败
- **THEN** 系统报告具体错误，保留原始数据，并拒绝使用不完整的迁移结果启动
