# 目录用途

`tests/unit/gateway/` 存放 Workspace Gateway 控制面、路由与运行时基础设施的单元测试。

## 职责索引

目录按生产边界下沉；根目录保留跨多个边界的合同和入口测试，查找时先按下面的职责树定位：

```text
tests/unit/gateway/
├── control/       Gateway 状态库、控制器、资源目录与导航队列
├── federation/    通道 owner、RPC 关闭与跨工作区路由
├── runtime/       子进程、端口、日志、重连与运行时锁
├── server/        静态 UI、代理路由、资源导航与启动恢复
└── 根目录横切测试
    ├── 配置与状态
    │   ├── test_gateway_config.py、test_gateway_state.py、test_gateway_sql_assets.py
    │   └── test_config_*.py
    ├── 凭据与身份
    │   ├── test_control_plane_auth.py、test_device_connections.py
    │   ├── test_private_credentials.py、test_gateway_route_credential_coverage.py
    │   ├── test_request_id.py
    │   └── test_user_access.py、test_user_profile.py
    ├── 工作区目标
    │   ├── test_registry.py、test_managed_workspaces.py、test_gateway_docker.py
    │   └── test_workspace_*.py
    ├── 联邦连接
    │   ├── test_federation*.py、test_gateway_identity_proxy_channels.py
    │   └── test_ssh_selection.py、test_remote_workspace_forward_cleanup.py
    ├── 代理与辅助资源
    │   ├── test_auxiliary_proxy.py、test_proxy_*.py、test_port_forwards.py
    │   └── test_directory_browser.py
    ├── API 组装
    │   ├── test_lifespan_http_pool.py、test_route_registration_order.py
    │   ├── test_runtime_consumer_health.py
    │   └── test_user_routes.py
    └── 展示与诊断
        └── test_diagnostics.py、test_themes.py、test_ui_settings.py、test_view_state.py
```

新增文件优先放到对应 owner 子目录；只有测试确实跨越多个边界时才留在根目录。成组移动测试时要同时迁移测试矩阵 suite 标识及 `out/tests/` 镜像产物路径，并检查相对导入和本地 fixture 查找。
当前根目录有 43 个直接 `test_*.py` 模块，已超过 20 个的拆分审查阈值；后续按上述职责成组迁移现有测试，避免继续扩张根层。
`test_registry.py` 当前 1,277 行，混合注册表持久化、远程投影交接、本机运行时租约和 DTO 健康探测，是优先拆分项。建议落到 `registry/`，按“持久化与重开、投影与租约、运行时所有权、DTO 健康快照”分文件；拆分时同步更新 `test_config_hot_reload_acceptance_matrix.py` 中的路径断言、测试矩阵选择和镜像产物路径。

## 可修改内容

- Gateway 单元测试和测试专用 fixture。
- 按生产模块边界继续拆分的测试子目录。

## 不可修改内容

- 不在此放置需要启动真实 Gateway 或工作区后端的端到端测试。
- 不写入真实用户的 `${BOXTEAM_HOME:-~/.boxteams}/` 控制面数据。

## 规范

- 测试必须使用临时目录隔离 Gateway 状态。
- 测试目录结构应与 `app/gateway/` 的生产模块边界对应。
