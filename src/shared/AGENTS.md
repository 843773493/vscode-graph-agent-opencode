# src/shared

## 目录用途

存放客户端和工作区辅助服务之间共享的跨进程传输定义。目前这里主要提供 SSE 字节流解析、帧边界处理、取消/超时语义、运行时校验及 Node 产品诊断日志。

## 可修改内容

- `sse.js`、`sseRuntime.js`：共享 SSE 传输和运行时 DTO 校验。
- `sseRuntimeValidators.js`：由协议生成流程维护的校验器。
- 与上述 JavaScript 实现对应的 `*.d.ts` 类型声明。
- `serverRuntimeLogging.mjs`：Launcher、Terminal 和 Browser 共用的 Node 产品日志合同、实例身份、等级及文本编码。只在服务端导入，不进入浏览器依赖图。
- Gateway 交付的 `BOXTEAM_RUNTIME_LOG_SINK` 仅提供冻结文件身份；进程状态同时核对实际 stdout/stderr 与当前文件的 device/inode，不能把环境中的路径当成已验证去向。未交付证明时 `log_sink` 为 null，不能据此断言 Launcher 没有文件 sink。
- `serverRuntimeLoggingControl.mjs`：复用 Gateway 私有凭据与既有服务端口／Launcher 控制通道的日志查询和应用。实际设置与 revision 只在日志入口保存；同实例、同 revision 重放幂等，冲突和过期确认明确拒绝。Launcher 应用确认复用其原 Logger 和文件 sink。

## 不可修改内容

- 不要在此目录添加页面、组件或平台宿主代码。
- 不要在此目录添加 Workspace 后端业务逻辑。
- 不要重新引入已经移除的 VS Code extension、Webview API 或旧客户端协议。

## 规范

- SSE 字节流统一由 `sse.js` 消费，业务模块不得维护第二套分帧逻辑。
- SSE JSON 必须通过 `sseRuntime.js` 中的运行时校验器校验，不能只依赖 TypeScript 类型断言。
- 生成文件 `sseRuntimeValidators.js` 和对应声明只能由协议生成流程覆盖。
- 共享模块不得依赖具体客户端入口；新增源码子目录必须包含四段式 `AGENTS.md`。
