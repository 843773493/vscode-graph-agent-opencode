# src/shared

## 目录用途

存放客户端和工作区辅助服务之间共享的跨进程传输定义。目前这里主要提供 SSE 字节流解析、帧边界处理、取消/超时语义、运行时校验、Node 产品诊断日志及受管插件的管理合同。

## 可修改内容

- `sse.js`、`sseRuntime.js`：共享 SSE 传输和运行时 DTO 校验。
- `sseRuntimeValidators.js`：由协议生成流程维护的校验器。
- 与上述 JavaScript 实现对应的 `*.d.ts`／`*.d.mts` 类型声明；服务端声明不依赖客户端入口。
- `serverRuntimeLogging.mjs`：Launcher、Terminal、Browser 和扩展管理服务共用的 Node 产品日志合同、实例身份、等级及文本编码。只在服务端导入，不进入浏览器依赖图。
- 受管 Node 首次使用日志入口前严格校验 `BOXTEAM_RUNTIME_LOGGING` 的 revision／level／pretty；之后读取实际 writer 状态，不重放启动值覆盖热应用。未受管入口仍使用共享合同默认值。
- Gateway 交付的 `BOXTEAM_RUNTIME_LOG_SINK` 仅提供冻结文件身份；进程状态同时核对实际 stdout/stderr 与当前路径打开后的句柄。device/inode 在 Windows 统一表示原生卷序列号和文件索引，其他平台使用 fstat；不能跳过设备身份或把环境中的路径当成已验证去向。Launcher 创建、重开和捕获接管共用 `runtimeLogFileIdentity`／`runtimeLogSinkMatches`。Launcher 通过 `registerRuntimeLogSink` 登记自己实际持有的日志描述符，登记成功后才提供诊断证明；未交付证明时 `log_sink` 为 null。
- `serverRuntimeControl.mjs`：复用 Gateway 私有凭据与既有服务端口／Launcher 控制通道的日志查询和应用。实际设置与 revision 只在日志入口保存；同实例、同 revision 重放幂等，冲突和过期确认明确拒绝。Launcher 应用确认复用其原 Logger 和文件 sink。辅助服务关闭共用本机鉴权，冻结 PID／工作区并在 ACK 后进入原 shutdown；回执不代表排空或进程退出。
- `serverPluginRuntime.mjs`：Node 插件读取 Gateway 交付的严格实例／端口／控制凭据，复用日志查询和应用，并提供 ready 身份回执和在 ACK 后调用服务原关闭函数的入口。仅声明 status／shutdown 能力；不提供 supervisor、实例账本、telemetry 或 statistics。
- `serverJsonRequest.mjs`：Terminal／Browser 共用的 JSON 解析及 HTTP 正文读取。Browser WebSocket 复用同一解析器，分别传入传输名、阶段及原服务 logger。空 HTTP 正文及正常 JSON 保持原语义；解析失败只公开阶段与类别，原异常写入已绑定工作区的服务日志并保留 cause，日志失败也不得覆盖原解析失败。
- `serverProcessLifecycle.mjs`：Terminal 与 Debug 共用的操作系统进程身份、进程树终止和实际退出核实；只在显式停止时读取进程事实，不参与普通业务调用。
- `serverHttpShutdown.mjs`：两类辅助服务共用的 HTTP 收尾，最多等待5秒；到期关闭原连接并抛出明确错误，宿主记录失败并退出1。普通关闭须先完成Manager排空，未核实则沿原管理重试链；致命错误仍按原合同退出1。传输收尾不是整个shutdown或外部资源的终止证明。

## 不可修改内容

- 不要在此目录添加页面、组件或平台宿主代码。
- 不要在此目录添加 Workspace 后端业务逻辑。
- 不要重新引入已经移除的 VS Code extension、Webview API 或旧客户端协议。

## 规范

- SSE 字节流统一由 `sse.js` 消费，业务模块不得维护第二套分帧逻辑。
- `readError` 仅映射实际 reader 读取失败，默认保留原异常；已出错流不再次取消，任何收尾出口均释放 reader 锁，取消失败保持可见。
- SSE JSON 必须通过 `sseRuntime.js` 中的运行时校验器校验，不能只依赖 TypeScript 类型断言。
- 生成文件 `sseRuntimeValidators.js` 和对应声明只能由协议生成流程覆盖。
- 共享模块不得依赖具体客户端入口；新增源码子目录必须包含四段式 `AGENTS.md`。
