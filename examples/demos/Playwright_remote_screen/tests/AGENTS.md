# 目录用途

`tests/` 存放 Playwright 远程屏幕实验项目的协议、命令解析和会话生命周期测试。

```text
tests/
├── animationFrameQueueFixture.js         可手动推进并检查取消的 requestAnimationFrame 队列
├── browserControlCommands.test.js        浏览器控制命令解析和 Playwright 动作映射
├── browserSession.test.js                screencast 并发启停和输入映射
├── browserSessionStub.js                 用假 CDP、页面和浏览器依赖构造 BrowserSession
├── canvasRectCache.test.js                同帧几何刷新合并和离散输入时效
├── cdpInputParameters.test.js             CDP 键盘、鼠标参数映射
├── clientMain.test.js                    浏览器入口 attach/detach 与待发 viewport 取消
├── httpRoutes.test.js                     命令 API、CDP HTTP 代理头过滤和断连释放
├── latestPointerMoveScheduler.test.js     指针移动合并、flush 与取消
├── protocol.test.js                      查看器消息协议校验
├── remoteFrameRenderer.test.js            最新帧合并、过期帧跳过和图像释放
└── webSocketRoutes.test.js               viewer attach 数量/回滚、帧背压、队列上限和共享序列化
```

# 可修改内容

- Bun 测试文件
- 只依赖实验项目公开函数的轻量单元测试

# 不可修改内容

- 不要在测试中启动主应用服务
- 不要把真实浏览器下载或系统环境配置写死到测试里

# 规范

- 测试应聚焦 HTTP/CDP 代理取消、CDP 输入参数映射、消息校验、命令解析、URL 规范化、指针调度、屏幕帧解码和 screencast 资源生命周期
- 浏览器入口测试使用轻量 DOM、Image、WebSocket 和可控计时器验证消息顺序，不启动真实浏览器
- 浏览器入口测试每个用例都重新创建 fake DOM、计时器和连接，并使用独立 ESM import URL；不能依赖前一个用例的模块级状态
- `canvasRectCache.test.js` 和 `latestPointerMoveScheduler.test.js` 共用 `animationFrameQueueFixture.js`，不要复制一套定时器假件
- 失败时直接暴露断言错误
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
