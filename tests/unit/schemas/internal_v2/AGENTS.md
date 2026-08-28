# tests/unit/schemas/internal_v2

## 目录用途

存放 `app/schemas/internal_v2/` 后端内部 V2 数据模型的单元测试。

## 可修改内容

- V2 schema 校验、OpenAPI 模型注册和序列化测试。
- 内部模型之间的静态一致性测试。

## 不可修改内容

- 不测试 Gateway HTTP 完整契约或真实 SSE 连接。
- 不混入内部事件模型测试。

## 规范

- HTTP/SSE 字段变动必须以显式断言体现。
- 错误输入必须断言具体校验失败，不接受虚假默认值。
