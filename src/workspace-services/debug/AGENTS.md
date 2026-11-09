# 目录用途

存放 workspace Debug 插件的目标运行服务源码。插件负责目标 Node 进程、Inspector WebSocket 和真实退出确认；调试业务状态由 Backend 原 owner 管理。

# 可修改内容

- 可以实现 Node Inspector 目标进程管理、插件运行时控制 WebSocket 及对应协议检查。
- 可以维护本目录及子目录的定向测试和四段式目录说明。

# 不可修改内容

- 不在插件中复制 Backend 的配置、断点、动作历史、权限或 SessionThread 业务状态。
- 不提供通用进程执行、任意 RPC 或未经声明的 Debug 能力。
- 不得把 WebSocket 断开当作目标进程已退出。

# 规范

- 使用 Node.js ESM，并将所有发行源码登记到 developer-tools 显式打包清单。
- 运行时消息必须使用固定版本合同；Inspector 方法采用显式白名单。
- 正常停止必须等待子进程实际退出；结果未知时不得自动重发 launch。
