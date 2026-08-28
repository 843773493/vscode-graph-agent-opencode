# 目录用途

`app/protocol/` 存放 Python 进程边界使用的公开协议绑定、codec 和 adapter。

## 可修改内容

- 可以维护 Python 协议生成入口、JSON/SSE/WS codec 和边界适配器。

## 不可修改内容

- 不要把 Agent 业务规则或 Workspace 内部事件总线实现放到这里。
- 不要手工修改 `generated/` 下的生成文件。

## 规范

- codec 负责线格式转换，adapter 负责连接业务模型与协议模型。
- 解析失败必须抛出带字段和边界信息的明确错误。
