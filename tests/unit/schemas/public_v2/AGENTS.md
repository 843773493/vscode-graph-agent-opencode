# tests/unit/schemas/public_v2

## 目录用途

存放 `app/schemas/public_v2/` 对外 V2 数据模型的单元测试。

## 可修改内容

- V2 schema 校验、OpenAPI 模型注册和序列化测试。
- 对外模型之间的静态一致性测试。

## 不可修改内容

- 不测试 Gateway HTTP 完整契约或真实 SSE 连接。
- 不混入内部事件模型测试。

## 规范

- 对外字段变动必须以显式断言体现。
- 错误输入必须断言具体校验失败，不接受虚假默认值。
