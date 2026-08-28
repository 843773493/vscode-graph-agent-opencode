# 目录用途

存放 `boxteam.workspace.message.v1` 的消息流事件和快照定义。

## 可修改内容

- 可以维护消息流生命周期、block/delta、ModelCall、ToolExecution、中断和恢复状态。

## 不可修改内容

- 不把 SSE 连接、订阅队列或工作区本地路径写进协议。
- 不直接暴露 LiteLLM raw chunk 和 provider 私有密钥/载荷。

## 规范

- 事件必须携带稳定关联键和单调事件序号。
- carrier 类型和生命周期字段必须能表达 LiteLLM AIMessage content carrier 的顺序。
