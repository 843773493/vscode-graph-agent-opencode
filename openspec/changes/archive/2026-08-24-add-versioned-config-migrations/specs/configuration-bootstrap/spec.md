## MODIFIED Requirements

### Requirement: 仅在缺失时初始化或升级配置
配置初始化 SHALL（必须）仅在用户配置不存在时创建配置；普通启动 MUST NOT（不得）完整重建或覆盖现有用户配置，但 SHALL 在加载前执行保留用户值的逐版本迁移。

#### Scenario: 安装后首次启动
- **WHEN** `boxteam` 启动且用户配置不存在
- **THEN** 内置生成器在 Gateway 启动前以原子方式创建带当前 `config_version`、稳定内置工具 ID 的配置及同目录 schema

#### Scenario: 配置已经是当前版本
- **WHEN** `boxteam` 启动且用户配置已经是当前版本
- **THEN** 启动流程验证并加载配置，不重写其内容

#### Scenario: 配置属于旧版本
- **WHEN** `boxteam` 启动且用户配置版本低于当前版本
- **THEN** 启动流程保留用户自定义值并原子升级该配置，然后验证和加载迁移结果

