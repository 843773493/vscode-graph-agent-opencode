## Purpose

为每个普通 Gateway 用户提供可独立管理的 profile 目录和明确的 Git 边界，使主题等个人配置拥有稳定落点，同时避免工作区、Gateway 连接、凭据和运行时数据进入未来的用户配置同步范围。

## ADDED Requirements

### Requirement: 用户 profile 目录初始化

系统 SHALL 在创建普通用户时初始化以稳定 `user_id` 命名的用户目录，并写入应用管理的 `.gitignore`；游客不得创建普通用户 profile 目录。

#### Scenario: 创建普通用户目录

- **WHEN** 普通用户创建成功
- **THEN** 用户目录存在，目录名严格等于 `user_id`，并包含默认 `.gitignore`

#### Scenario: 游客访问

- **WHEN** 游客进入工作区
- **THEN** 系统不创建普通用户目录，也不把游客追踪数据写入可作为用户配置同步源的目录

### Requirement: 用户配置边界

用户 profile 目录 SHALL 只承载用户主题、布局和其他可同步个人偏好；工作区数据、Gateway 连接、凭据、SQLite 数据库、游客记录和运行时缓存不得作为用户 profile 内容。

#### Scenario: 主题保存

- **WHEN** 用户保存个人主题
- **THEN** 主题写入该用户 profile 范围，并且其他普通用户读取不到该主题作为自己的默认主题

#### Scenario: 非 profile 数据隔离

- **WHEN** 系统生成工作区连接、凭据或运行时状态
- **THEN** 这些数据写入各自的 Gateway 或 Workspace 状态范围，不出现在用户 profile 目录中

### Requirement: 本次变更不执行 Git 同步

本次系统 SHALL 只初始化 `.gitignore` 和 profile 文件边界，不得自动初始化远程仓库、保存远程 URL、执行拉取/推送或处理 Git 冲突。

#### Scenario: 用户目录已初始化

- **WHEN** 用户查看新建的 profile 目录
- **THEN** 目录可被用户自行作为 Git 工作树使用，但系统没有创建或修改任何远程同步配置

#### Scenario: Git 同步范围

- **WHEN** 后续用户手动将 profile 目录加入 Git
- **THEN** 默认忽略规则排除运行时、凭据、连接、工作区和数据库等非 profile 数据
