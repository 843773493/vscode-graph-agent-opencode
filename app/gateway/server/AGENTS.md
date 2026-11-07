# 目录用途

`app/gateway/server/` 存放 Gateway 服务启动装配与透明代理路由实现。

# 可修改内容

- Gateway 注册表和托管运行时的启动恢复流程
- 工作区 API 的 HTTP/SSE 透明代理路由
- 独立Node宿主通道与实时目标发现的现有认证、原生管理授权、路由引用和失效检查；共用同一路由解析，只传原协议字节，不启动插件进程。发现不启动Backend、不以离线镜像代替实时状态。
- `extension_configuration.py`透明转发正式具名配置资源的stat/read/write/events；client从原用户租约派生，保留generation／view／有序工作区身份，披露与原心跳重新核对host及configuration读写权限。配置文件与共享watcher仍归目标Backend，不在Gateway建立配置副本或保存基线。
- 仅服务于 Gateway 入口装配的辅助函数
- `extension_management.py`是常驻原生管理组件的具名本机控制入口，核原运行时凭据、loopback、安装域及已登记操作；不接受浏览器Origin、联邦凭据或任意method。目标级库存、卸载直接调用安装组件，包字节由Gateway Python在原串行准入内读取；VSIX来源与运行实例仍由Backend原owner核验。

# 不可修改内容

- 不实现 Agent、会话或工具业务逻辑
- 不直接读写工作区 `.boxteam/` 业务数据
- 不在此目录维护浏览器前端展示状态

# 规范

- 启动失败和代理失败必须返回或抛出明确错误
- SSH 与本地工作区的持久化权威来源仍由 Gateway registry 管理
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
