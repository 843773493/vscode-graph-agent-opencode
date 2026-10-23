# 目录用途

`src/workspace-services/terminal/client/` 存放独立终端 attach 网页。用户从主前端资源视图打开该页面后，可以连接指定终端、断开连接、输入命令并查看实时输出。

`server.js`是Node静态资源入口；工作区UUID与权威身份文件核对只依赖`../server/workspaceIdentity.js`，不为校验加载TerminalManager、Session或PTY管理链。网页main.js不导入这个Node模块。

监听地址是传给Node的主机值，URL中的主机则须按authority语法表示：显式IPv6地址在请求解析基址和启动日志中都通过既有`formatUrlHost()`加方括号；`auto`后端地址继续从HTTP Host解析。HTTP入口验证位于`tests/integration/harness/test_process_ownership.py`，只证明静态服务，不替代页面交互或PTY业务验收。

`main.js`从`./shared/commandText.js`读取唯一的命令文本过滤器；静态服务只公开该明确模块，不开放shared目录的任意文件。页面在输入边界沿用空值到空串的转换，原始输出、sequence及ACK合同不因展示过滤改变；后端提供display_buffer时直接消费该字段。

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
