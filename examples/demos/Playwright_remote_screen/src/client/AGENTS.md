# 目录用途

`src/client/` 存放 Playwright 远程屏幕实验项目的浏览器页面、输入转发逻辑和样式。

# 可修改内容

- `index.html`
- `main.js`
- `style.css`

# 不可修改内容

- 不要在浏览器端保存 Playwright 会话权威状态
- 不要加入构建产物或外部 CDN 依赖
- 不要把失败状态隐藏在 UI 之外

# 规范

- 页面必须提供 attach/detach 到 `uuid:12` 的明确按钮
- 鼠标、滚轮、键盘、粘贴和导航都通过 WebSocket 发往服务端
- UI 错误状态必须可见
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
