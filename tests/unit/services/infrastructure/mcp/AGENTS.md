# 目录用途

存放 MCP 基础设施配置、命名、工具发现和调用适配的单元测试。

# 可修改内容

- MCP 配置解析、环境变量展开和 URL 安全校验测试。
- MCP 工具命名与运行时 Manager 测试。

# 不可修改内容

- 不连接真实外部 MCP Server。
- 不访问用户 Codex 配置或真实网络。

# 规范

- 依赖统一通过 pytest fixture 注入。
- 文件系统使用 `tmp_path` 隔离。
- 真实 stdio Server 连接只放在 `tests/e2e/mcp/`。
