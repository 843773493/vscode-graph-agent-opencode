# 目录用途

`src/workspace-services/browser/client/` 存放独立浏览器 attach 网页。用户从主前端资源视图打开该页面后，可以连接指定浏览器页面、断开连接、输入 URL、点击远程屏幕并查看实时画面。

`browserElementContext.js`只格式化元素选择的剪贴板正文：普通模式保留HTML，rich模式生成既定Markdown；元素显示标签优先取路径末项，伪元素附在所属元素后，无路径时才解析选择字段。该标签是CSS形状的说明文字，不是可执行selector、元素ref或资源URI；定位与引用校验仍归后端页面动作owner，不从显示文字反推身份。

页面客户端按输入、展示与交互控制器组织：

```text
client/
├── main.js                       页面装配、会话状态、消息生命周期和帧绘制
├── browserInputEvents.js         输入事件绑定、连续点击和连续输入合并
├── browserToolbarEvents.js       地址栏、导航与设备配置操作
├── browserModalUi.js             浏览器对话框与文件选择交互
├── browserCollaborationUi.js     多用户指针、复制与协作展示
├── browserShortcuts.js           attach 页面的快捷键
├── browserDebugElementTree.js    DOM 检查树渲染
├── browserClientUtils.js         坐标换算、按钮和修饰键的纯转换
└── browserElementContext.js      DOM 元素选择的剪贴板格式
```

`main.js` 持有页面运行态并连接协议与控制器；新增功能优先放入对应的同级职责模块，避免主入口继续膨胀。

HTML 的早期地址提交处理在样式与模块加载前安装到 document；初始化期间回车只保存待导航地址，不能触发表单原生跳转而清空 Gateway／工作区／Browser 参数。完整模块装配后仍由原工具栏与同一 navigate 链路接手。
`browserDebugElementTree.js` 以迭代顺序构建 DOM 检查树，并限制单次可见行数为 2,000；超出时显示明确的截断提示，避免把宽 DOM 快照全部铺成大型页面节点树。

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
