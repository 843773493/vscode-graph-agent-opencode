# 目录用途

目标 Backend 的 Node Extension Host 进程与原生 IPC 生命周期；沿固定上游协议传输。

# 可修改内容

- 已选定运行时的直接启动、私有 IPC、输出收集及原进程退出核验。
- Backend registry唯一装配宿主owner；关闭先撤销宿主准入并回收IPC/进程，再排空工作区依赖。
- 专用运行时从 Launcher runtime manifest 的 extension_host 声明解析；缺失表示不可用，不借应用通用 Node 或 PATH。
- `describe`与实际连接共用Backend／mount校验和准入事实；读取版本／平台／通道状态不创建session、不重新探测或扫描runtime。通道状态不代表完整扩展API已经接入。
- `management.py`只监督同一专用runtime中的原生扩展管理组件；Code OSS安装／profile／scanner服务持有唯一安装事实，Python不扫描目录或保存清单。每条请求等待稳定Backend安装域的portalocker，创建新管理进程，等原初始化、业务、异步drain和正常退出后释放；多个Backend进程共用原profile，普通宿主发现不启动它。
- 包只读控制沿同一管理组件，以默认 profile、原 ExtensionKey 和包内文件段定位保留版本；不依赖当前 profile 的最新成员，不接收任意原生路径或建立发布 token 账本。公开授权及浏览器资源传输须另行接通，私有读取能力不授予访问权限。
- `configuration.py`只将`source_uri.py`正式登记的目标profile settings与多根view配置映射到原Node FileService；文件stat/read/write不建立第二配置writer或保存基线。配置观察复用共享`WorkspaceFileWatchService`，仅订阅两个已登记文件的直接父目录并精确过滤。

# 不可修改内容

- 不实现另一套扩展 RPC、安装器、工作区注册表或调用租约。
- 不把内部可执行路径作为未经授权的公开启动参数。

# 规范

- 子进程统一由 OwnedChildProcess 持有；取消、断连与启动失败均等待实际回收。
- IPC 只传原协议字节，不在此解析、重放或改写扩展调用。
- Linux沿私有Unix socket，Windows沿runtime的单连接named pipe；平台差异不改变宿主owner、原协议、日志或进程退出核验。macOS未验时明确拒绝，不能改用公开TCP。
- 公开传输先登记settled占用；owner通知stopping并等待传输双向任务结清，才关闭IPC和实际进程。
- 关闭Backend须同时等待管理进程结清。成功与明确业务错误都必须取得drained并核实原进程正常退出；取消／控制流失败后回收原进程组并拒绝自动重放安装，后续冷读由新owner读取原profile。真实退出、日志关闭或解锁失败时保留原owner供close重试。
- 配置stat/read/write只从原session冻结的稳定view身份、有序roots与native URI恢复新Node进程的内存登记，不调用prepare或重建被删除的workspace配置。
- 管理授权、稳定目标与 view 必须在公开入口接线时完成；本层不提供 HTTP 入口。
- 目标 VSIX 来源由原文件 owner 解析已挂接工作区的文件 URI，保留私有目录保护；安装与库存消费同一 Node 控制回执，不启动 Agent 容器或另建安装事实。
- Gateway独立通道认证、原生管理权限和持续撤权归`app/gateway/server/extension_host.py`；Backend私有入口归`app/api/extension_host.py`与`extension_configuration.py`。配置操作必须匹配已认证client、generation、view和原mount中的有序工作区集合；认证、集合校验或局部配置验证不能替代完整窗口隔离验收。
