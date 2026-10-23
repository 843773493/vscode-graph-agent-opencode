# 目录用途

`src/workspace-services/browser/client/` 存放独立浏览器 attach 网页。用户从主前端资源视图打开该页面后，可以连接指定浏览器页面、断开连接、输入 URL、点击远程屏幕并查看实时画面。

`server.js`是Node静态资源入口；工作区UUID格式校验只依赖`../server/workspaceIdentity.js`，不加载BrowserManager或Playwright。它仍读取并核对显式工作区的权威身份文件；这个Node模块不进入网页main.js的浏览器依赖图。

监听地址是传给Node的主机值，URL中的主机则须按authority语法表示：显式IPv6地址在请求解析基址和启动日志中都通过既有`formatUrlHost()`加方括号；`auto`后端地址继续从HTTP Host解析。HTTP入口验证位于`tests/integration/harness/test_process_ownership.py`，只证明静态服务，不替代页面交互或Browser业务后端验收。

`browserElementContext.js`只格式化元素选择的剪贴板正文：普通模式保留HTML，rich模式生成既定Markdown；元素显示标签优先取路径末项，伪元素附在所属元素后，无路径时才解析选择字段。该标签是CSS形状的说明文字，不是可执行selector、元素ref或资源URI；定位与引用校验仍归后端页面动作owner，不从显示文字反推身份。

输入与画面的职责分布：

```text
client/
├── browserClientUtils.js  坐标换算、按钮与修饰键的纯转换
├── browserInputEvents.js  输入事件绑定、连续点击和连续输入合并
└── main.js                页面状态、画面解码绘制及帧确认
```

`clickedAtPerformanceMs`、`decodeStartedAtPerformanceMs` 和 `drawStartedAtPerformanceMs` 都来自当前页面的 `performance.now()`，只能在同一页面时钟内求差；`decodeMs/drawMs` 是传给后端的耗时，不是事件时间。`frameDecodeSequence` 则是本地解码结果的失效序号，开始解码和切页都会递增；不能用于后端 `frameId` 确认、资源身份或跨页面排序。

同一画面有三种尺寸与位置：`remoteViewport` 是远端页面的 CSS 视口，`canvas.width/height` 是截图的后备像素尺寸，`viewportDisplayPosition` 是本地滚动容器内容区中的 CSS 显示偏移。输入链通过 `getRemoteViewport()` 明确传入远端视口，再由 `remotePointFromEvent()` 换算；不能把本地拖动画面的位置当作远端指针坐标，也不能把 DPR 重复乘入指针坐标。该工具函数省略目标视口时仍使用画布后备尺寸，返回单位相应不同。`clampViewportDisplayPosition()` 每次只读取一次画面布局矩形，限制本地拖动仍保留 12 CSS 像素的可见边缘，不持有跨事件的几何缓存。

# 可修改内容

- 可以新增或修改 HTML、CSS、浏览器端 JavaScript。
- 可以调整浏览器 attach/detach 交互、状态提示、错误展示和远程屏幕控制。
- 可以维护原生浏览器 API 与 WebSocket 协议集成。

# 不可修改内容

- 不要把主聊天 UI 或 React 状态管理写到这里。
- 不要依赖 VS Code webview 宿主能力。
- 不要把浏览器端代码写成需要构建才能运行的形式。

# 规范

- 使用浏览器原生 JavaScript，避免引入构建步骤。
- UI 必须显示连接状态和错误信息。
- attach/detach 不应关闭后台浏览器页面。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
