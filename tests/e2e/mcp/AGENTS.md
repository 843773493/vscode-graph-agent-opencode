# 目录用途

存放 BoxTeam 连接真实 MCP Server 的端到端测试，以及测试专用的最小 MCP Server 和配置辅助函数。

# 可修改内容

- MCP stdio 与 Streamable HTTP E2E。
- 测试专用 MCP Server。
- Codex MCP Server 配置探测和隔离工作区配置生成。

# 不可修改内容

- 不修改用户 Codex 配置。
- 不在项目根目录、`asset/` 或本目录写入测试运行产物。
- 不把用户凭据复制进测试日志或长期测试文件。

# 规范

- 每个测试文件独立启动工作区后端。
- 每个正式 MCP E2E 的工作区和日志写入 `out/tests/e2e/mcp/<测试文件名>/`，分别使用 `workspace/` 和 `artifacts/` 子目录。
- `out/tests/temp/` 仅供测试脚本之外的 Agent 临时诊断使用。
- 外部 MCP Server 不存在或无法启动时必须明确跳过，不能伪造成功。
- mini MCP Server 只提供确定性、无外部副作用的测试工具。
