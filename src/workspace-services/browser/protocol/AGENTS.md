# 目录用途

存放 Browser Manager HTTP/JSON 与 WebSocket 消息的协议 adapter。

## 可修改内容

- 可以维护页面操作、输入、帧、下载和状态消息的线格式转换。

## 不可修改内容

- 不得在这里实现 Playwright 操作或浏览器资源调度。

## 规范

- 协议错误必须包含消息类型和字段名称。
- 二进制帧与 JSON 控制消息必须保持清晰分界。
