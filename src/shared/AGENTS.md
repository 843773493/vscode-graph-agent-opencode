# src/shared

## 目录作用

前端各模块共享的常量、API 客户端和通信协议定义。后端的 HTTP API 封装、SSE 事件流解析、VS Code Webview 消息类型定义集中于此。

如果你主要会后端，可以把这里理解成“前端和扩展 Host 都要一起看的公共说明书”：

- `constants.js` 放默认值和常量
- `api.js` 负责怎么跟本地后端 HTTP/SSE 通信
- `sse.js` 负责标准 SSE 字节流、帧边界和取消/超时语义
- `sseRuntime.js` 与生成的 validator 负责后端 SSE DTO 运行时校验
- `protocol.js` 负责 Webview 和扩展 Host 之间能发哪些消息

## 可以修改

- `api.js`：后端 API 调用函数
- `sse.js`、`sseRuntime.js`：共享 SSE 传输与 DTO 校验
- `*.d.ts`：与共享 JavaScript 实现对应的 TypeScript 类型入口
- `constants.js`：常量和默认配置
- `protocol.js`：Webview 与 Host 之间的消息协议类型

## 不要修改

- 不要在此目录添加 UI 渲染代码
- 不要在此目录添加后端进程管理代码
- 不要包含环境变量硬编码（如 token 仅用于本地开发）

## 约定

- 所有常量使用 `export const` 命名导出
- API 函数统一通过 `requestJson` 封装，自动处理 headers、错误和 JSON 解析
- SSE 字节流统一由 `consumeSseResponse` 消费，帧统一由 `parseSseFrameBlock` 解析；业务 API 不得再维护第二套分帧逻辑
- SSE JSON 必须通过 `sseRuntime.js` 中由后端 Pydantic schema 生成的 validator 校验，不能只靠 TypeScript 类型断言
- 生成文件 `sseRuntimeValidators.js` 与 `sseRuntimeValidators.d.ts` 只能由 `bun run gen:protocol` 覆盖
- 协议类型分为 `HostToWebviewMessageType` 和 `WebviewToHostMessageType` 两组
- 新增 API 或消息类型时，必须同步更新此目录
- 这里不放 UI，不放扩展生命周期管理，只放“共享定义”
