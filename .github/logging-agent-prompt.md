# Prompt：实现可插拔日志系统（支持 VS Code OutputChannel）

你是当前项目的资深 TypeScript 工程师。  
请为一个前后端分离项目实现一套 **可插拔、多目标输出、宿主无关** 的日志系统。

项目背景如下：

- 后端是运行在工作区里的 `server`
- 前端宿主可选，例如 VS Code 插件
- `server` 当前需要支持：
  - 本地日志持久化
  - 终端打印
- 当 `server` 由 VS Code 插件启动时，还需要支持：
  - 额外输出到 VS Code 的 Output 面板
- 设计目标是：
  - `server` **不能直接依赖 `vscode` 包**
  - VS Code 输出能力必须通过 **bridge / adapter** 注入
  - 日志系统必须支持后续扩展更多宿主或 sink
  - 业务代码不能感知“写到哪里”，只调用统一 logger 接口

---

## 一、总体设计原则

请严格遵循以下原则：

1. **Logger 负责收集和分发日志事件，不直接关心具体输出目标**
2. 输出目标统一抽象为 **Sink**
3. `server` 默认内置：
   - `FileSink`
   - `TerminalSink`
4. `VS Code OutputChannel` 不属于 server 内建能力，而是由宿主通过 **bridge sink** 注入
5. 所有日志采用 **结构化日志事件**
6. 业务代码只能写：
   - `logger.trace(...)`
   - `logger.debug(...)`
   - `logger.info(...)`
   - `logger.warn(...)`
   - `logger.error(...)`
   不允许在业务层传 `toFile` / `toTerminal` / `toVSCode` 之类的路由参数
7. Sink 自己决定是否消费日志，例如按 level、scope、配置过滤
8. 整个系统必须支持后续扩展：
   - 其他 IDE 宿主
   - 远程日志上报
   - 测试时的内存 sink
   - 诊断包导出

---

## 二、目标架构

实现下面这类架构：

```txt
business code
   -> logger
      -> FileSink
      -> TerminalSink
      -> ExtensionBridgeSink (optional)
            -> host adapter / IPC / callback
                  -> VS Code OutputChannel
```

其中：

- `Logger` 是日志入口
- `LogEvent` 是统一事件模型
- `LogSink` 是输出接口
- `ExtensionBridgeSink` 是 server 侧 bridge sink
- VS Code 插件侧持有 `OutputChannel`
- `server` 不可直接 import `vscode`

---

## 三、数据模型要求

请定义统一日志级别：

```ts
type LogLevel = "trace" | "debug" | "info" | "warn" | "error"
```

请定义结构化日志事件：

```ts
interface LogEvent {
  ts: number
  level: LogLevel
  message: string
  scope?: string
  data?: unknown
  err?: {
    name?: string
    message: string
    stack?: string
  }
  sessionId?: string
  requestId?: string
}
```

要求：

1. `ts` 为毫秒时间戳
2. `message` 为主消息
3. `scope` 用于标记日志来源，例如：
   - `server`
   - `rpc`
   - `task`
   - `auth`
   - `fs`
4. `data` 用于承载结构化附加上下文
5. `err` 必须是已归一化的错误对象，不能直接把原始 Error 任意塞进去
6. 所有 sink 统一接收 `LogEvent`

---

## 四、接口设计要求

### 1. LogSink

实现统一 sink 接口：

```ts
interface LogSink {
  write(event: LogEvent): void | Promise<void>
  flush?(): Promise<void>
  dispose?(): Promise<void>
}
```

说明：

- `write` 是必需
- `flush` 用于 server 退出前收尾
- `dispose` 用于释放资源

### 2. Logger

实现 `Logger` 类，要求：

- 内部维护多个 sink
- 支持动态注册和卸载 sink
- 支持 level 过滤
- 提供统一的便捷方法：
  - `trace`
  - `debug`
  - `info`
  - `warn`
  - `error`
- 允许传入 `scope` 和 `data`
- `error` 方法要支持 error 归一化
- 单个 sink 失败不能影响其他 sink
- 必须避免因为 sink 异常导致主流程崩溃

建议接口形态：

```ts
interface LogContext {
  scope?: string
  data?: unknown
  sessionId?: string
  requestId?: string
}
```

示例调用风格：

```ts
logger.info("server started", { scope: "server", data: { port } })
logger.warn("retrying request", { scope: "rpc", data: { attempt } })
logger.error("rpc failed", err, { scope: "rpc", requestId, data: { method } })
```

---

## 五、必须实现的组件

请至少实现以下模块。

### 1. `normalizeError`

实现错误归一化工具：

```ts
function normalizeError(err: unknown): LogEvent["err"]
```

要求：

- 若是 `Error`，提取 `name`、`message`、`stack`
- 若是字符串，转成 `{ message: string }`
- 若是普通对象，尽量安全提取
- 不允许归一化过程再抛异常

### 2. `formatForHuman`

实现面向终端 / OutputChannel 的人类可读格式化函数：

```ts
function formatForHuman(event: LogEvent): string
```

建议输出格式：

```txt
[2026-04-07 10:15:23.123] INFO [rpc] request started {"method":"task.run","requestId":"abc"}
```

要求：

- 包含时间
- 包含 level
- 有 scope 时显示 scope
- 有 data 时尽量紧凑序列化
- 有 err 时输出错误 message，必要时附 stack
- 保持可读性
- 不要依赖颜色库，避免宿主耦合

### 3. `FileSink`

实现文件持久化 sink。

要求：

1. 采用 **JSON Lines** 格式，每条日志一行 JSON
2. 每次写入追加到文件
3. 适合长期诊断和机器读取
4. 尽量避免频繁打开关闭文件造成性能问题
5. 至少支持：
   - 指定文件路径
   - flush
   - dispose
6. 写文件失败时不能导致 logger 崩溃，但应尽量上报内部错误

输出示例：

```json
{"ts":1712480000000,"level":"info","message":"server started","scope":"server","data":{"port":3000}}
```

### 4. `TerminalSink`

实现终端输出 sink。

要求：

1. 使用 `formatForHuman(event)` 输出
2. `warn` 和 `error` 走 `stderr`
3. 其他级别走 `stdout`
4. 支持最小日志级别过滤
5. 不做持久化

### 5. `ExtensionBridgeSink`

实现 server 侧 bridge sink。

定义 bridge 接口：

```ts
interface LogBridge {
  emitLog(event: LogEvent): void
}
```

`ExtensionBridgeSink` 要求：

1. 接收一个 `LogBridge`
2. 接收可配置的最小日志级别
3. 满足条件时把事件转发给 `bridge.emitLog(event)`
4. 若 bridge 抛错，不影响其他 sink
5. 不依赖 `vscode`

---

## 六、宿主集成要求

请按下面方式设计宿主接入。

### server 侧

定义宿主适配器：

```ts
interface ServerHostAdapters {
  logBridge?: LogBridge
}
```

在 `createServer` 或 server 初始化函数中：

1. 创建 logger
2. 默认挂载：
   - `FileSink`
   - `TerminalSink`
3. 如果存在 `adapters.logBridge`
   - 额外挂载 `ExtensionBridgeSink`

伪代码：

```ts
function createServer(adapters: ServerHostAdapters = {}) {
  const logger = new Logger()

  logger.addSink(new FileSink(logFilePath))
  logger.addSink(new TerminalSink())

  if (adapters.logBridge) {
    logger.addSink(new ExtensionBridgeSink(adapters.logBridge, "debug"))
  }

  return { logger, ... }
}
```

### VS Code 插件侧

VS Code 插件不在本次 server 核心实现里，但请预留好对接方式。

预期接入方式如下：

```ts
const channel = vscode.window.createOutputChannel("Kilo Code")

const server = createServer({
  logBridge: {
    emitLog(event) {
      channel.appendLine(formatForHuman(event))
    }
  }
})
```

要求：

- server 本身不知道 `OutputChannel`
- server 只知道 bridge
- OutputChannel 生命周期由插件控制

---

## 七、配置与过滤要求

请支持日志过滤，不要把路由控制放到业务代码参数里。

建议支持以下过滤方式：

1. **全局最小级别**
   - 例如 `info`
2. **每个 sink 自己的最小级别**
   - FileSink: `debug`
   - TerminalSink: `info`
   - ExtensionBridgeSink: `debug`
3. 可选支持 `scope` 过滤
   - 例如只把 `rpc`、`task` 相关日志发到某 sink

至少先实现：
- 全局 level 过滤
- sink 级别过滤

业务代码必须保持简单，不得这样设计：

```ts
logger.info("x", { toFile: true, toTerminal: false })
```

这种设计是禁止的。

---

## 八、非功能要求

实现时请满足以下工程要求：

1. 使用 TypeScript
2. 尽量保持模块职责清晰
3. 不引入不必要的重型依赖
4. 不要让某个 sink 的异常中断主业务
5. 代码应便于单元测试
6. 命名清晰，接口收敛
7. 保持宿主无关性
8. 文件结构清晰
9. 优先实现最小可用版本，但代码要可扩展

---

## 九、推荐文件结构

请按类似结构组织代码：

```txt
src/logging/
  types.ts
  levels.ts
  normalizeError.ts
  formatForHuman.ts
  Logger.ts
  sinks/
    FileSink.ts
    TerminalSink.ts
    ExtensionBridgeSink.ts
  index.ts
```

如有需要可补充：

```txt
src/server/
  createServer.ts
```

---

## 十、建议实现细节

请遵循以下实现偏好：

### 1. 级别比较

实现一个级别优先级表，例如：

```ts
const LOG_LEVEL_PRIORITY = {
  trace: 10,
  debug: 20,
  info: 30,
  warn: 40,
  error: 50,
}
```

并提供：

```ts
function shouldLog(current: LogLevel, min: LogLevel): boolean
```

### 2. Logger 容错

`Logger.log()` 在遍历 sinks 时：

- 不能因为某个 sink 失败而中断
- 可以吞掉 sink 错误，或通过内部 fallback 上报
- 不能递归调用自身导致死循环

### 3. 文件格式

持久化优先选 **JSONL**

原因：
- 适合 grep / tail
- 适合后续上传诊断包
- 适合机器分析

### 4. 面向人类的展示

终端和 VS Code 面板都用 `formatForHuman`

这样可以保持显示风格一致。

### 5. 生命周期

请确保 sink 可在 server 退出时执行：

- `flush`
- `dispose`

并在 server shutdown 流程里调用。

---

## 十一、产出要求

请直接产出以下内容：

1. 完整 TypeScript 实现代码
2. 必要的类型定义
3. `createServer` 集成示例
4. VS Code 宿主对接示例
5. 一个简短使用示例，展示：
   - 记录 info 日志
   - 记录 warn 日志
   - 记录 error 日志
6. 如有必要，补充最少量注释说明设计意图

---

## 十二、明确禁止事项

请不要这样做：

1. 不要让 `server` 直接 import `vscode`
2. 不要把 OutputChannel 逻辑写进核心 logger
3. 不要让业务代码关心日志写到哪里
4. 不要只记录字符串，必须保留结构化事件
5. 不要让单个 sink 的写入失败拖垮整个 logger
6. 不要把“本地持久化”和“临时展示”混成同一份不可扩展逻辑
7. 不要依赖大量外部框架来完成一个基础日志系统

---

## 十三、验收标准

只有满足以下条件才算完成：

- 有统一 `LogEvent`
- 有统一 `LogSink`
- 有 `Logger`
- 有 `FileSink`
- 有 `TerminalSink`
- 有 `ExtensionBridgeSink`
- `server` 默认文件 + 终端
- VS Code 场景可通过 bridge 额外挂 OutputChannel
- `server` 不依赖 `vscode`
- 业务层调用简单统一
- 支持后续扩展更多 sink
