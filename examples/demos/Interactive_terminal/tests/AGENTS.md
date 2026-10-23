# 目录用途

`tests/` 存放交互式终端原型的 Bun 单元测试。

```text
tests/
├── clientMain.test.js        attach/detach UI 状态、resize 合并与尺寸去重、WebSocket 失败状态
├── protocol.test.js          WebSocket 消息类型和必填字段
└── terminalManager.test.js   Shell 平台选择、共享 PTY、尺寸去重和失败重试、退出清理、输出扇出和输入校验
```

# 可修改内容

- TerminalManager 测试
- WebSocket 协议校验测试
- 原型相关测试辅助对象

# 不可修改内容

- 不要在这里启动真实长期终端进程
- 不要修改主仓库测试目录

# 规范

- 使用 fake PTY 测试终端管理行为
- 浏览器入口测试使用轻量 DOM、xterm 和 WebSocket stub，不启动真实终端服务
- 浏览器入口测试每个用例都重建 fake DOM、计时器和连接，并使用独立 ESM import URL；不能依赖前一个用例的模块级状态
- 窗口 resize 使用可控计时器验证 80 ms 合并；尺寸不变时只读取一次布局，尺寸变化时只拟合并发送一次 PTY resize
- 多客户端输出测试只核对相同冻结事件对象；实际 WebSocket 广播在 `server.js` 用弱映射按事件缓存编码字符串
- 慢客户端的 8 MiB 发送缓冲上限由 `server.js` 检查，超限后关闭连接以释放 PTY 输出订阅
- 重复尺寸只断言不会触发 PTY 调用，尺寸改变后只触发一次
- PTY 退出后检查数据和退出监听器都已解除
- 测试非法输入时要断言快速失败
- 测试名称表达原型行为
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
