# 目录用途

`app/gateway/server/` 存放 Gateway 服务启动装配与透明代理路由实现。

# 可修改内容

- Gateway 注册表和托管运行时的启动恢复流程
- 工作区 API 的 HTTP/SSE 透明代理路由
- 独立Node宿主通道与实时目标发现的现有认证、原生管理授权、路由引用和失效检查；共用同一路由解析，只传原协议字节，不启动插件进程。发现不启动Backend、不以离线镜像代替实时状态。
- `extension_configuration.py`透明转发正式具名配置资源的stat/read/write/events；client从原用户租约派生，保留generation／view／有序工作区身份，披露与原心跳重新核对host及configuration读写权限。配置文件与共享watcher仍归目标Backend，不在Gateway建立配置副本或保存基线。Node／Web共用原安装域路由，空工作区视图仍明确绑定Backend与generation；连接选择只在本跳消费，远端回执披露前重核原连接身份。
- 仅服务于 Gateway 入口装配的辅助函数
- `extension_management.py`是常驻原生管理组件的具名本机控制入口，核原运行时凭据、loopback、安装域及已登记操作；不接受浏览器Origin、联邦凭据或任意method。目标级库存、卸载直接调用安装组件，包字节由Gateway Python在原串行准入内读取；VSIX来源与运行实例仍由Backend原owner核验。
- 市场安装与 metadata 写入同库存沿唯一目标安装域路由，不要求工作区或运行宿主；写入核安装管理权限，Relay 消费并剥离本跳连接选择，回执披露前重核目标与原 profile。市场下载沿原安装请求能力，不引入第二安装入口或浏览器权威。
- 其下载回调须绑定尚未结清的原安装能力与request_id，终态撤销后拒绝旧回调；只允许原安装器具名制品callSite，不提供通用fetch、文件写入或独立服务。回执未知期间不能因普通健康检查拒绝原已受理安装结清下载。
- `extension_package.py`的浏览器字节入口与原POST共用授权、目标路由和Python读取链；HTTP路径保留view／generation／精确包键及显式联邦连接，使相对资源仍属同一实例。同源浏览器可沿原HttpOnly用户租约读取属于该用户的已准入实例，不把管理凭据放入URL；显式凭据仍须校验，不回落cookie。每次披露重核运行权限，不因安装权限或浏览器缓存继续披露退役实例正文。

# 不可修改内容

- 不实现 Agent、会话或工具业务逻辑
- 不直接读写工作区 `.boxteam/` 业务数据
- 不在此目录维护浏览器前端展示状态

# 规范

- 启动失败和代理失败必须返回或抛出明确错误
- SSH 与本地工作区的持久化权威来源仍由 Gateway registry 管理
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

- `runner.py`是正式Gateway HTTP进程的唯一入口；从原ASGI lifespan state取得同一扩展管理owner，在Uvicorn关闭listener前等待quiesce。启动未完成不结清；结清失败保留原异常和非零退出，不创建第二管理owner。
