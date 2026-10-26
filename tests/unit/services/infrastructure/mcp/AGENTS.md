# 目录用途

存放 MCP 基础设施配置、命名、工具发现和调用适配的单元测试。

```text
mcp/
├── AGENTS.md
├── test_catalog_activation.py          # catalog 成员向资源平台发布与激活
├── test_catalog_owner.py               # owner 生命周期、刷新、generation 与事件
├── test_config.py                      # MCP 配置规范化与校验
├── test_extension_catalog_binding.py   # 冻结 binding、namespace 与目标解析
├── test_naming.py                      # MCP server/tool 命名合同
├── test_session_lifecycle.py           # Server 原 task、取消、失败关闭及通知失效
├── test_invocation_leases.py           # 精确调用占用、冷启动核对与写盘失败
├── test_tool_adapter.py                # SDK 完整分页、原参数与协议结果转换
├── test_tool_definition_recovery.py    # 发现、压缩、策略、冷读与 rewind 边界
├── test_tool_search.py                 # 冻结定义、策略、排名、字节边界、分页和冷恢复
└── test_tool_guidance.py               # 工具指引输出与语义
```

需要 `EventChannelService` 的测试从上级 `infrastructure/conftest.py` 注入隔离实例。

# 可修改内容

- MCP 配置解析、环境变量展开和 URL 安全校验测试。
- MCP 工具命名与运行时 Manager 测试。

# 不可修改内容

- 不连接真实外部 MCP Server。
- 不访问用户 Codex 配置或真实网络。

# 规范

- 依赖统一通过 pytest fixture 注入。
- 文件系统使用 `tmp_path` 隔离。
- 真实 stdio Server 连接只放在 `tests/e2e/system/workspace_services/mcp/`。
