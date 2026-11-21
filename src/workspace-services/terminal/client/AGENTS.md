# 目录用途

`src/workspace-services/terminal/client/` 存放独立终端 attach 网页。用户从主前端资源视图打开该页面后，可以连接指定终端、断开连接、输入命令并查看实时输出。

页面与依赖由目标 Gateway 的 `/api/gateway/attach/terminal/` 直接提供，不再启动独立 Node 静态进程。页面必须带明确 Gateway、工作区和资源身份；业务操作沿原 Workspace owner API，交互 WebSocket 使用 transport grant 返回的精确插件实例服务引用。

`main.js`从`./shared/commandText.js`读取唯一的命令文本过滤器；静态服务只公开该明确模块，不开放shared目录的任意文件。页面在输入边界沿用空值到空串的转换，原始输出、sequence及ACK合同不因展示过滤改变；后端提供display_buffer时直接消费该字段。

页面只在实际可见时保持输出连接和资源观察；嵌入页面通过同源宿主转发通知，复用宿主同工作区的共享 WebSocket，独立页面复用 `src/shared/realtime.js` 建立自己的页面级 WS。按资源ID和页面订阅身份过滤，隐藏或卸载解除登记；原生 IntersectionObserver 覆盖 iframe 祖先隐藏，Page Visibility 覆盖文档隐藏。隐藏释放传输但保留用户的连接意图，恢复沿原 sequence 补读；不停止后台 PTY。状态通知复用工作区资源 owner 事件流，首次/重连和真实变化才从同一 owner 的 `include_output=false` 摘要补读，终态转换时补读最终正文；不按心跳或输出chunk刷新清单。首次打开、显式刷新及 WebSocket 重同步仍取得正文，传输授权保留原资源投影。摘要补读串行合并，不并发堆积请求。

`terminalConnection.js`唯一持有字节WebSocket、连接意图、重连计时器、ACK游标及已发送尺寸；`main.js`取得原owner授权、处理资源操作和页面展示，不复制连接状态。断开只释放传输，不停止PTY；迟到的旧socket事件不得影响当前连接。

ACK游标只保留在当前页面内存中；重载页面沿owner完整快照重建屏幕和游标，不逐包写入浏览器存储。等待授权时的连接／断开以最新用户意图为准，不因合并授权请求丢失重连意图。

## 可修改内容

- 可以新增或修改 HTML、CSS、浏览器端 JavaScript。
- 可以调整终端 attach/detach 交互、状态提示和错误展示。
- 可以维护 xterm 相关前端集成。

## 不可修改内容

- 不要把主聊天 UI 或 React 状态管理写到这里。
- 不要依赖 VS Code webview 宿主能力。
- 不要把浏览器端代码写成需要构建才能运行的形式。

## 规范

- 使用浏览器原生 JavaScript，避免引入构建步骤。
- UI 必须显示连接状态和错误信息。
- attach/detach 不应杀死后台终端。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
