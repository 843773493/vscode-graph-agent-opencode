# 目录用途

目标 Backend 的 Node Extension Host 进程与原生 IPC 生命周期；沿固定上游协议传输。

# 可修改内容

- 已选定运行时的直接启动、私有 IPC、输出收集及原进程退出核验。
- Backend registry唯一装配宿主owner；关闭先撤销宿主准入并回收IPC/进程，再排空工作区依赖。
- 专用运行时从 Launcher runtime manifest 的 extension_host 声明解析；缺失表示不可用，不借应用通用 Node 或 PATH。
- `describe`与实际连接共用Backend／mount校验和准入事实；读取版本／平台／通道状态不创建session、不重新探测或扫描runtime。通道状态不代表完整扩展API已经接入。
- `management.py`只通过具名本机认证入口调用原生 Gateway 已启动的常驻管理组件，不拥有进程；Code OSS安装／profile／scanner服务持有唯一安装事实，Python不扫描目录或保存清单。管理进程、安装域锁与日志归`app/gateway/runtime/extension_management.py`；多个Backend共用同一服务，读取不启动或下载组件。
- Web／Node 运行引用由原 session 向已认证的常驻安装实例登记，引用端点冻结该服务地址和代际，Gateway 换代不截断结清。Web 在同一次原 scanner/profile 准入取得精确发布描述；Node 由真实 registry 在首次运行与增量发布前提供实际用户扩展集合。不能将全部库存当作运行集合，或把引用能力放入普通宿主 DTO。
- Web stat/read 由 Backend 直接调用唯一的 `package_files.publication_file`，不再逐文件调用 Gateway／Node。安装详情的 current-only 请求仍在原 Node 发布流内即时核当前 profile，由 Gateway Python 调用同一字节读取函数；不能用库存 mirror 授权或创建第二套读取实现。
- Web 关闭先撤准入，等待公开传输和已开始的 Python 同步 I/O 真正结清，读后再核原 client/view/generation/profile/key/逻辑 ID，撤权或取消不得披露迟到正文。Node 必须核实真实 child 退出，再释放该代引用；失败时保留原 session 与 HTTP 客户端供显式结清。管理组件崩溃走服务组／应用恢复，不要求全域退出证明或持久引用对账；正常完整停机在引用与读取结清后沿原安装器清理旧包，清包不阻塞恢复，仍安装插件的持久存储保持。
- Web 连接发布独立业务 context，只含目标平台、具名 profile 设置和稳定工作区视图，不包含 Node 进程初始化。未挂接目录的 Web 视图仍可访问该目标 profile；Node 准入继续要求非空根集合。Node／Web 配置共用原 session、客户端、代际校验和 Python 文件／watcher owner，原生环境随同次发布描述返回，不增加逐配置 Node 请求。
- `configuration.py`直接读写`source_uri.py`正式登记的目标 profile settings 与多根 view 文件；原环境仅提供 profile／untitled workspace 位置描述。稳定 view 以现有无覆盖原子发布原语首次物化，已有文件只验冻结有序根并保留 JSONC 字节，编辑、dirty 与 etag 仍归原 Code OSS。配置观察复用共享`WorkspaceFileWatchService`，仅订阅两个已登记文件的直接父目录并精确过滤。

# 不可修改内容

- 不实现另一套扩展 RPC、安装器、工作区注册表或调用租约。
- 不把内部可执行路径作为未经授权的公开启动参数。

# 规范

- 子进程统一由 OwnedChildProcess 持有；取消、断连与启动失败均等待实际回收。
- IPC 只传原协议字节，不在此解析、重放或改写扩展调用。
- Linux沿私有Unix socket，Windows沿runtime的单连接named pipe；平台差异不改变宿主owner、原协议、日志或进程退出核验。macOS未验时明确拒绝，不能改用公开TCP。
- 公开传输先登记settled占用；owner通知stopping并等待传输双向任务结清，才关闭IPC和实际进程。
- 关闭Backend只结清其管理HTTP客户端与运行宿主，不关闭共享管理进程。控制回执丢失须明确报告且不重放安装；Gateway换代只在已接受调用结清后释放句柄，完整停机才排空并核管理进程退出；安装域锁始终由管理服务持有。
- 配置 stat/read/write 只消费原 session 冻结的稳定 view 与有序 roots，不经过管理 Node，不建立额外保存基线；只有 owner 初始化可首次物化 view，stat/read 不重建已删除文件。同步 I/O 复用 run_sync_shielded，取消必须等待原 worker 结清；跨 Backend 沿同一目标磁盘文件和原生文件锁保持读写可见。
- 独立设置通过同一 owner 的 prepare 获取明确 profile 与稳定视图，不创建运行宿主或配置 session；后续请求用 profile 归属，扩展 API 请求仍用原宿主 generation，两种归属互斥。必要原生环境描述在 Backend 生命周期内复用，正文及保存基线不缓存；独立设置初始化和文件 worker 与关闭共用原配置锁。
- 管理授权、稳定目标与 view 必须在公开入口接线时完成；本层不提供 HTTP 入口。
- 目标 VSIX 来源由原文件 owner 解析已挂接工作区的文件 URI，保留私有目录保护；安装与库存消费同一 Node 控制回执，不启动 Agent 容器或另建安装事实。
- Gateway独立通道认证、原生管理权限和持续撤权归`app/gateway/server/extension_host.py`；Backend私有入口归`app/api/extension_host.py`与`extension_configuration.py`。配置操作必须匹配已认证client、generation、view和原mount中的有序工作区集合；认证、集合校验或局部配置验证不能替代完整窗口隔离验收。
