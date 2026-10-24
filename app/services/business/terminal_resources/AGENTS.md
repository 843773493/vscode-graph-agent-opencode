# 目录用途

工作区 Terminal 资源的唯一领域 owner，负责来源 Thread、保留范围、关联、版本、租约引用和恢复事实。

## 文件职责树

```text
terminal_resources/
├── owner.py       Terminal 领域状态与生命周期，委托共享资源 owner 处理通用保留和释放
└── operations.py  单次受信 Terminal 调用，持有 operation lease 并核实外部回执
```

调用路径从 `app/agents/tools/terminal.py` 进入 `TerminalResourceOwner.operation()`，再由 `TerminalOperations` 执行单次调用；Browser 与 Terminal 共有的 lease 或记录规则归 `workspace_resources/`，PTY 事实和终端动作则留在本目录。

## 可修改内容

- 修改 Terminal 创建、查询、管理、Thread 卸载、Session 删除与孤儿回收。
- 修改 owner 持久记录和外部 PTY 事实核实。

## 不可修改内容

- 不实现 PTY、HTTP、WebSocket 或 Gateway 代理。
- 不创建第二套 operation lease 账本，不保留旧 Node 领域 owner 或迁移双轨。

## 规范

- 显式绑定工作区 UUID 与根目录；真实 SessionThread 必须经过 catalog 和生命周期准入。
- 创建依赖已装配的 `WorkspaceFilePaths`；所有 Terminal 创建入口在 owner 内解析 cwd。目录 URI 复用该文件 owner 的目标/权限校验，原生 Shell 路径继续允许工作区外和私有目录；不改写命令正文。工具回执直接使用真实 PTY 的 `cwd_relative`，不另建 cwd 投影或按工具参数猜测执行位置。
- 受管 cwd 准入由 `TerminalWorkingDirectories` 提供；在准入临界区内先保存目录根 URI，并在既有账本发布绑定终端身份的持久目录 lease，再启动 PTY。工具/Turn 返回不释放该 lease；只在原 owner 核实真实终态后结清，冷恢复沿同一规则处理，不能把缺失或未知回执当作停止。
- 外部结果未知时保留可发现记录并显式报错；停止/删除只在核实后结清。
- 持久化不包含绝对真实路径，客户端连接不隐式建立 Thread 关联。
