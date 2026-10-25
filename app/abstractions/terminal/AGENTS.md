# 目录用途

`app/abstractions/terminal/` 存放 Terminal 资源 owner 与持久执行观察之间共享的协议。

```text
terminal/
├── resources.py          Terminal 快照、请求错误与 PTY 传输端口
└── execution_monitor.py  持久终端操作与孤儿资源观察端口
```

# 可修改内容

- 可以调整 Terminal 资源传输、状态观察所需的 Protocol 和轻量类型。
- 可以按调用方语义收紧协议参数，减少模糊字典和弱类型字段。

# 不可修改内容

- 不在这里实现 PTY、HTTP 客户端、业务 owner 或执行编排。
- 不依赖具体 Gateway、Workspace 或存储实现。

# 规范

- 协议名称表达调用方需要的能力；具体实现留在业务或基础设施 owner。
- 修改协议后检查全部实现方和调用点，不保留旧模块路径的转发别名。
