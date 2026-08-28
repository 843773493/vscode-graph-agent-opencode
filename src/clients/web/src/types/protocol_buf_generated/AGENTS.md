# 目录用途

`protocol_buf_generated/` 存放由 Buf ES 插件生成的 Web Protobuf schema 和运行时绑定。

# 可修改内容

- 只能通过 `buf.gen.yaml` 与 `proto/` 协议源重新生成内容。

# 不可修改内容

- 不得直接编辑生成的 `.js` 或 `.d.ts` 文件。
- 不得在这里放手写业务类型或浏览器状态。

# 规范

- 生成绑定只负责 Protobuf 编解码；JSON 业务类型使用同级 `protocol_generated/` 的 ts-proto 绑定。
