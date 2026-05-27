# src/shared

## 目录作用

前端各模块共享的常量、API 客户端和通信协议定义。后端的 HTTP API 封装、SSE 事件流解析、VS Code Webview 消息类型定义集中于此。

如果你主要会后端，可以把这里理解成“前端和扩展 Host 都要一起看的公共说明书”：

- `constants.js` 放默认值和常量
- `api.js` 负责怎么跟本地后端 HTTP/SSE 通信
- `protocol.js` 负责 Webview 和扩展 Host 之间能发哪些消息

## 可以修改

- `api.js`：后端 API 调用函数
- `constants.js`：常量和默认配置
- `protocol.js`：Webview 与 Host 之间的消息协议类型

## 不要修改

- 不要在此目录添加 UI 渲染代码
- 不要在此目录添加后端进程管理代码
- 不要包含环境变量硬编码（如 token 仅用于本地开发）

## 约定

- 所有常量使用 `export const` 命名导出
- API 函数统一通过 `requestJson` 封装，自动处理 headers、错误和 JSON 解析
- SSE 解析函数 `parseSseBlock` 按标准 SSE 格式（`event:` / `data:` / `:` 注释）解析
- 协议类型分为 `HostToWebviewMessageType` 和 `WebviewToHostMessageType` 两组
- 新增 API 或消息类型时，必须同步更新此目录
- 这里不放 UI，不放扩展生命周期管理，只放“共享定义”
