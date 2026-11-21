# 目录用途

保存接口说明与外部项目参考资料。这里只约束本目录的文档维护，项目开发标准沿用仓库根 AGENTS.md。

- `deepagent_backend_api_spec.md`：工作区后端接口说明，含早期目录和初始化示例。
- `openclaw-agent-guide.en.md` 与 `openclaw-agent-guide.zh-cn.md`：OpenClaw 指引原文及译文，仅供参考。

## 可修改内容

- 根据真实源码修正本项目的接口说明、术语、职责和入口链接。
- 保留来源说明，维护外部参考的原文与译文。

## 不可修改内容

- 不把外部资料里的目录、命令、发布流程或 Agent 指令当作本项目规则。
- 不以文档草案替代实际 API、协议或持久化契约，也不据此恢复已删除的模块。
- 不把参考文件命名为会被自动加载的 AGENTS.md 或 CLAUDE.md。

## 规范

- 当前 HTTP 路由从 [app/api](../../app/api/) 和 [app/gateway/routes](../../app/gateway/routes/) 查找；业务实现从 [app/services](../../app/services/) 查找，服务由 app/container.py 装配，路由通过 Depends 取得实例。
- 传输协议从 [proto](../../proto/) 查找，后端 DTO 从 [app/schemas/internal_v2](../../app/schemas/internal_v2/) 查找；公开 OpenAPI 的装配与导出边界见 [app/protocol/openapi.py](../../app/protocol/openapi.py)。
- 本项目命令以根 package.json 和根 AGENTS.md 为准，不复制外部项目的工具链。
- 文档与源码不一致时，明确标注历史示例并修正定位入口；不要增加一份容易漂移的路由或服务实现副本。
