# 目录用途

原 MCP ClientSession 的唯一 stdio 传输，持有实际子进程和官方消息流。

# 可修改内容

- stdio 字节流分帧、官方消息类型序列化及原进程生命周期交付。

# 不可修改内容

- 不创建第二个 MCP client、JSON-RPC 编码器、调用租约或安装账本。
- 不调用 SDK 私有符号，不添加旧传输兼容分支或启动包装脚本。

# 规范

- 消息解析及输出只用官方 JSONRPCMessage／SessionMessage 类型。
- session 上下文仍由原 runtime task 创建和退出；取消须排空原进程清理。
