# 目录用途

存放 Python Protobuf 与现有 HTTP JSON、SSE、WebSocket JSON 之间的转换代码。

## 可修改内容

- 可以新增明确的字段映射、动态 Struct 转换和错误转换。

## 不可修改内容

- 不要在 codec 中实现业务流程或改变事件顺序。

## 规范

- 动态对象不能静默丢字段；不支持的值必须报错。
