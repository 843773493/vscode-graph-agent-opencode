## 1. 静态配置资源

- [x] 1.1 从现有 schema 拆出 `gateway_config.jsonc` 与 `workspace_config.jsonc`，收紧跨域字段并建立独立 v1
- [x] 1.2 用静态 JSONC 实现普通及开发 Gateway/Workspace 模板，并验证 `$schema` 和开发差异
- [x] 1.3 删除动态 defaults 与 development overlay，简化开发 SSH 资产模块及其命名

## 2. 安装与路径

- [x] 2.1 集中定义用户级 Gateway/Workspace、schema 和工作区覆盖路径，移除旧路径运行时 fallback
- [x] 2.2 重构配置安装 CLI/installer，使普通初始化和 force 操作复制双配置静态资源
- [x] 2.3 更新 `scripts/dev.mjs`，在启动服务前原子安装 `.env`、两个开发配置和两个 schema

## 3. 运行时加载边界

- [x] 3.1 实现 Gateway 专属配置加载器，只解析和验证用户级 `gateway.jsonc`
- [x] 3.2 收窄 Workspace `ConfigService`，只合并用户级与当前显式工作区的 `workspace.jsonc`
- [x] 3.3 更新 Gateway、Workspace、CLI 和热重载调用方，不再使用统一 `boxteam.jsonc` 或源码配置

## 4. 布局与版本迁移

- [x] 4.1 实现旧用户级合并配置到 Gateway/Workspace v1 的预验证、状态记录和幂等提交
- [x] 4.2 实现旧工作区配置改名迁移，并拒绝工作区中的 Gateway 字段及新旧布局冲突
- [x] 4.3 更新 migrate/doctor 命令和迁移日志，覆盖中断恢复、冲突与旧 v1–v4 fixture

## 5. 发行与测试适配

- [x] 5.1 更新 npm runtime manifest、staging 和搬迁 smoke test，使发行包携带双配置及 schema
- [x] 5.2 更新单元/E2E fixture 和隔离工作区配置文件名，覆盖 Gateway 不读取工作区配置
- [x] 5.3 更新 AGENTS、启动说明和配置引用，清除动态生成器、overlay、旧文件名及环境绕过残留

## 6. 验证

- [x] 6.1 运行 OpenSpec 校验、Python 静态分析和配置/Gateway/Workspace 相关单元测试
- [x] 6.2 验证源码开发配置安装及 Web 全链路，确认运行时只访问 `BOXTEAM_HOME` 与显式工作区配置
- [x] 6.3 运行项目架构审查，处理目录职责、文件组织和单文件膨胀问题
