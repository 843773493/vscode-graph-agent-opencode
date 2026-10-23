# 目录用途

`app/gateway/theme/` 承载 Gateway 控制面拥有的 Web UI 主题配置、解析、校验、背景资源与引用管理能力。

# 文件职责树

```text
theme/
├── __init__.py       延迟公开包 API
├── models.py         主题定义的数据模型
├── defaults.py       默认主题配置值
├── builtins.py       暖色主题 token
├── immersive.py      沉浸式主题 token
├── validation.py     token 与背景显示规则校验
├── config_store.py   已校验主题配置的读取和快照
├── resolver.py       主题定义解析、设置主题解析与目录生成
├── assets.py         背景图片的导入、读取、删除和元数据管理
└── references.py     主题对背景图片的引用统计与同步
```

主题配置沿“token 定义 → 校验与配置快照 → 解析为主题目录”的方向流动；图片资源由 `assets.py` 管理物理资源，由 `references.py` 同步主题引用，避免把配置解析和文件生命周期混在一起。

# 可修改内容

- 内置主题 token 与主题解析规则。
- Gateway 主题配置快照和校验逻辑。
- Gateway UI 图片资源的导入、读取、删除与引用同步。
- 对 `app.gateway.theme` 公开边界的导出。

# 不可修改内容

- 不实现工作区 Agent 业务逻辑，也不读写工作区 `.boxteam/` 业务数据。
- 不在此目录实现前端页面、交互或状态管理。
- 不绕过 Gateway 配置和 UI 资源的既有存储路径。

# 规范

- 对外调用统一通过 `app.gateway.theme` 包导出，内部模块保持单一职责。
- 配置或资源无效时必须明确抛错，不得静默降级或返回虚假默认值。
- Gateway 级主题配置由控制面负责；用户上传的背景资源必须通过 profile 资源根目录读写，不能跨用户共享。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
