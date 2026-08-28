# 目录用途

存放 Terminal Manager HTTP/JSON 与 WebSocket 消息的协议 adapter。

## 可修改内容

- 可以维护 attach、输入、resize、输出、状态和 ACK 的线格式转换。

## 不可修改内容

- 不得在这里实现 PTY 生命周期或终端状态持久化。

## 规范

- 协议错误必须包含消息类型和字段名称。
- 终端业务对象与生成绑定通过显式转换连接。
