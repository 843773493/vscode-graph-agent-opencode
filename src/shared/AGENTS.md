# src/shared

## 目录用途

存放客户端和工作区辅助服务之间共享的跨进程传输定义。目前这里主要提供 SSE 字节流解析、帧边界处理、取消/超时语义和运行时校验。

## 可修改内容

- `sse.js`、`sseRuntime.js`：共享 SSE 传输和运行时 DTO 校验。
- `sseRuntimeValidators.js`：由协议生成流程维护的校验器。
- 与上述 JavaScript 实现对应的 `*.d.ts` 类型声明。

## 不可修改内容

- 不要在此目录添加页面、组件或平台宿主代码。
- 不要在此目录添加 Workspace 后端业务逻辑。
- 不要重新引入已经移除的 VS Code extension、Webview API 或旧客户端协议。

## 规范

- SSE 字节流统一由 `sse.js` 消费，业务模块不得维护第二套分帧逻辑。
- SSE JSON 必须通过 `sseRuntime.js` 中的运行时校验器校验，不能只依赖 TypeScript 类型断言。
- 生成文件 `sseRuntimeValidators.js` 和对应声明只能由协议生成流程覆盖。
- 共享模块不得依赖具体客户端入口；新增源码子目录必须包含四段式 `AGENTS.md`。
