# 目录用途

存放 Python Protobuf 与现有 HTTP JSON、SSE、WebSocket JSON 之间的转换代码。

## 可修改内容

- 可以新增明确的字段映射、动态 Struct 转换和错误转换。

## 不可修改内容

- 不要在 codec 中实现业务流程或改变事件顺序。

## 规范

- 动态对象不能静默丢字段；不支持的值必须报错。
- 消息流信封的关联身份由 descriptor 的可选字符串字段推导，编解码和 payload 身份核对共用同一结果；`job_id` 保留独立传输的业务合同，不从 payload 补齐或参与关联身份冲突检查。
- 消息流快照的64位整数从实际 protobuf 字段还原，嵌套消息和数组复用同一遍历；保留 presence，不按字段名猜测或改写 Struct 动态正文。
