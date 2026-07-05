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
- 用户输入和 agent 输入都通过 WebSocket 发往服务端
- UI 错误状态必须可见
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
