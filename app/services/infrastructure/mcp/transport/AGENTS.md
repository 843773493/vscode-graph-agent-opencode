# 目录用途

Gateway 自有 MCP stdio 插件桥复用的官方 stdio 编解码与进程启动实现。

# 可修改内容

- stdio 字节流分帧、官方消息类型序列化及插件实例子进程生命周期交付。

# 不可修改内容

- 不创建第二个 MCP client、JSON-RPC 编码器、调用租约或安装账本。
- 不调用 SDK 私有符号，不添加旧传输兼容分支或启动包装脚本。

# 规范

- 消息解析及输出只用官方 JSONRPCMessage／SessionMessage 类型。
- 子进程由 Gateway 插件实例生命周期持有；Backend 通过 MCP Streamable HTTP 连接插件服务。
