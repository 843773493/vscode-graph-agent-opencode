# 目录用途

`src/client/` 存放 Playwright 远程屏幕实验项目的浏览器页面、输入转发逻辑和样式。

```text
client/
├── index.html             远程屏幕工作台页面
├── target.html            被远程浏览器打开的演示目标页及其独立样式
├── main.js                WebSocket 生命周期、输入顺序和页面状态
├── latestPointerMoveScheduler.js 按动画帧合并指针移动，离散输入前 flush
├── canvasRectCache.js     合并滚动和 resize 的布局读取，离散输入时同步刷新
├── remoteFrameRenderer.js 串行解码并绘制最新屏幕帧
└── style.css              工作台页面样式
```

# 可修改内容

- `index.html` 中的远程屏幕工作台结构
- `target.html` 中被远程浏览器打开的演示目标页
- `main.js` 中的 WebSocket 生命周期、输入转发和页面状态
- `latestPointerMoveScheduler.js` 中的高频指针合并与离散输入前 flush
- `remoteFrameRenderer.js` 中的远程帧解码与绘制
- `style.css` 中的工作台样式；目标页样式内嵌在 `target.html`

# 不可修改内容

- 不要在浏览器端保存 Playwright 会话权威状态
- 不要加入构建产物或外部 CDN 依赖
- 不要把失败状态隐藏在 UI 之外

# 规范

- 页面必须提供 attach/detach 到 `uuid:12` 的明确按钮
- 同时最多保留一个未关闭的 WebSocket；延迟到达的旧 socket 事件不能覆盖当前连接状态
- 鼠标、滚轮、键盘、粘贴和导航都通过 WebSocket 发往服务端
- 帧解码状态和画布绘制由 `remoteFrameRenderer.js` 管理
- 高频 pointermove 复用缓存的 canvas 边界；尺寸、滚动和窗口变化时刷新，按下/抬起/滚轮前读取最新边界
- 滚动、resize 和 ResizeObserver 在同一动画帧内只触发一次 canvas 边界读取；离散输入立即读取最新值
- 每个动画帧最多发送一个 pointermove；按下/抬起/滚轮、键盘、粘贴、命令和 detach 前先发送最新位置
- viewport resize 合并 80 ms；detach、断连、连接错误和页面卸载时取消待发 viewport
- 帧绘制或解码失败后释放 `Image` 上一帧的 data URL，避免空闲时长期保留大帧资源
- UI 错误状态必须可见
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
