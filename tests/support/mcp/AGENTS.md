# 目录用途

集中存放测试专用 MCP 配置准备和 session 协议替身。

```text
mcp/
├── configuration.py   写入隔离工作区 MCP 配置
└── session_doubles.py MCP session、工厂和 relist 控制替身
```

# 可修改内容

- 测试 MCP 配置文件准备。
- 通过 pytest fixture 注入的 MCP session fake 和工厂。

# 不可修改内容

- 不放生产 MCP 实现或测试用例。
- 不把真实 MCP stdio server 移入本目录；其入口位于 `../e2e_control/mcp_stdio_server.py`。

# 规范

- 配置目标必须由调用测试显式指定，不能写入 fixture 源目录。
- fake 只提供测试明确要求的协议行为，未实现的请求必须显式失败。
