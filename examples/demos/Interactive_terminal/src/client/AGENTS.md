# 目录用途

`src/client/` 存放交互式终端原型的浏览器页面、xterm 初始化逻辑和样式。

# 可修改内容

- `index.html`
- `main.js`
- `style.css`

# 不可修改内容

- 不要在浏览器端保存终端进程状态
- 不要加入构建产物或外部 CDN 依赖

# 规范

- 页面必须提供 attach/detach 到 `uuid:12` 的明确按钮
- 同时最多保留一个未关闭的 WebSocket；连接回调只更新创建该回调的当前 socket
- 用户输入和 agent 输入都通过 WebSocket 发往服务端
- 窗口 resize 事件合并 80 ms，只向 PTY 发送变化后的列数和行数；attach 握手时同步最新尺寸
- UI 错误状态必须可见
- 服务端发出 error 消息后即使随后关闭 WebSocket，也必须保留具体错误，不得被通用断连状态覆盖
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
