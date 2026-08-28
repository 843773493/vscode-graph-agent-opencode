# 目录用途

存放 Workspace 消息流的独立版本化公共协议。

## 可修改内容

- 可以新增消息流事件、block carrier、工具执行和恢复快照字段。

## 不可修改内容

- 不放 Agent 内部事件总线实现、文件存储细节或 provider 私有结构。
- 不把旧 `boxteam.workspace.v2` 事件语义复制为兼容字段。

## 规范

- 消息流协议按 `v1` 目录隔离，并使用 `boxteam.workspace.message.v1` package。
- 已发布字段编号不得复用；不再使用的字段必须保留并标记为 reserved。
