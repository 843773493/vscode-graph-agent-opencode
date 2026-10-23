# 目录用途

`src/server/` 存放实验项目的 Node HTTP 服务、Playwright 会话管理、CDP screencast 和 WebSocket 协议入口。

```text
server/
├── browserSession.js   Playwright 页面、CDP 输入、屏幕帧生命周期和按平台选择的 Chromium 参数
├── cdpInputParameters.js 将键盘、鼠标消息映射为 CDP 参数
├── httpRoutes.js       静态资源、命令 API、可取消且剔除逐跳响应头的 CDP HTTP 代理
├── browserControlCommandClient.js 从 CLI 向远程屏幕服务发送浏览器控制命令
├── protocol.js         WebSocket 消息校验与类型
├── server.js           启动配置、HTTP/WebSocket 监听和关闭
├── browserControlCommands.js 浏览器控制命令解析及 Playwright 动作映射
├── webSocketRoutes.js  查看器消息和 CDP WebSocket 代理
└── url.js              HTTP 地址规范化
```

# 可修改内容

- HTTP 静态资源服务
- WebSocket 消息校验和路由
- Playwright 浏览器生命周期、浏览器控制命令和 CDP 输入事件

# 不可修改内容

- 不要接入主应用 FastAPI 服务
- 不要增加数据库、消息队列或云服务依赖
- 不要把页面失败伪装成成功状态

# 规范

- 使用 Node.js 内置模块和明确依赖
- 对非法 browserId、非法消息和缺失字段快速失败
- `detach` 只断开 Web 查看器，不关闭 Playwright 浏览器
- 浏览器启动中途失败或 HTTP 端口绑定失败时必须关闭已启动的 Chromium；清理失败应与原始启动错误一起抛出
- 关闭时并行停止 HTTP/WebSocket 接入；HTTP 连接最多宽限 1 秒，WebSocket 向客户端发送 1001 并宽限 1 秒，之后分别终止残留连接
- 关闭时即使停止 screencast 失败，也要继续关闭 Chromium；HTTP、WebSocket 和浏览器清理错误必须显式报告
- 无 attach 客户端时，导航、DOM 和 load 事件不查询页面标题并重复广播状态
- 无 attach 客户端时，收到的 screencast 尾帧只发送 CDP 确认，不构造或广播浏览器帧对象
- attach 期间的导航、DOM 和 load 状态更新在 50 ms 内合并；命令结果仍即时返回
- 命令本身已经生成页面状态快照时，HTTP/WebSocket 响应复用该快照，避免重复调用 Playwright 查询
- 多个查看器并发 attach/detach 和 viewport 更新时，screencast 启停按会话串行，并根据最新客户端集合收敛
- attach 启动失败时，撤销路由订阅并移除该客户端登记，使同一连接可重试
- socket 关闭时停止接收新控制消息，并将 detach 清理排到正在执行的消息之后，避免 attach 尚未登记完成就提前清理；断连期间的操作或清理错误仍须写入服务日志
- 命令 API 的无效 JSON/命令使用 400，超过 64 KiB 的请求体使用 413；限制内存时仍完整消费请求流
- CDP WebSocket 上游连接期间的待发送消息最多缓存 1 MiB；任一方向的 socket 发送缓冲最多 8 MiB，超限会关闭该代理连接并释放缓存
- CDP HTTP 响应按 Node 流背压转发；响应头发出后发生传输错误时关闭连接，不整包复制 DevTools 资源
- CDP HTTP 客户端断开时取消尚未完成的上游请求并释放响应流
- CDP WebSocket 代理原样转发 Buffer/ArrayBuffer payload；不为统计大小或转发文本帧进行 UTF-8 解码与重编码
- 查看器帧按帧对象共享序列化结果；每个 socket 的发送缓冲最多 8 MiB，慢客户端丢弃过期屏幕帧，控制消息无法入队时关闭连接
- 发送屏幕帧前，以 base64 data URL 长度作为完整 JSON 消息长度下界；待发送缓冲加下界达到 8 MiB 时直接丢帧，避免编码确定无法发送的过期帧
- 固定 ASCII browserId 的帧 JSON 可用字符串长度 O(1) 取得 UTF-8 字节数；自定义 Unicode browserId 必须回退精确计算，不能让缓冲上限少计
- 查看器控制消息顺序执行，待处理队列最多 2048 条且累计 8 MiB，超限时关闭连接并丢弃尚未执行的消息
- Chromium 的 `/dev/shm` 与沙箱启动参数只在 Linux 上启用；Windows 和 macOS 使用默认配置
- `browserLaunchArgs` 的平台参数单测只证明参数分流，不能代替各平台实际启动 Playwright Chromium
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
