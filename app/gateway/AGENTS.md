# 目录用途

`app/gateway/` 存放独立的本地 Gateway 服务。Gateway 负责管理多个工作区目标，并把浏览器前端的 API 请求转发到当前激活的工作区后端。

## 职责树

```text
app/gateway/
├── main.py + lifespan.py + route_registry.py   应用装配、启动恢复、路由注册
├── routes/                                      Gateway 自有 HTTP API，按资源分组
├── server/                                      启动装配、工作区 HTTP/SSE 透明代理
├── protocol/                                    Gateway 控制面协议与代理元数据转换
├── registry/                                    工作区目标注册、持久化、路由租约和投影
├── runtime/                                     本机/SSH 工作区进程、端口和生命周期
├── control/                                     Gateway 全局状态、配置应用和控制面 API
│   ├── gateway_state/                           全局状态库及配置应用账本
│   ├── gateway_registry/                        全局工作区目录账本
│   ├── gateway_config_source/                   配置来源和版本账本
│   ├── gateway_config_events/                   配置事件投递账本
│   └── gateway_runtime_generation/              运行配置代次和健康证明
├── federation/                                  Gateway 间身份、授权、通道与 RPC
├── config/                                      Gateway 配置读取、校验和热重载
├── theme/                                       Gateway 控制面的主题和图片资源
└── 根层模块                                     鉴权、凭据、SSH、设备连接、代理等横切能力
```

按数据归属和调用方向选目录：新增 Gateway 自有 URL 放 `routes/` 并由 `route_registry.py` 登记；
工作区业务请求的透明转发放 `server/` 或 `auxiliary_proxy.py`；工作区身份、来源、目标和路由租约
归 `registry/`；启动、停止和监视运行进程归 `runtime/`；Gateway 全局配置与账本归 `control/`；
跨 Gateway 的身份与 RPC 归 `federation/`。根层文件只保留跨这些边界复用的能力，不在代理层复制工作区业务规则。

根层模块索引：`auth.py` 校验本地与联邦访问凭据；`credentials.py` 管理 Gateway 联邦凭据和身份；
`federation_pairing.py` 提供凭据签发命令；`managed_workspaces.py` 和 `remote_gateway.py` 协调直接及远端
工作区目标；`ssh_command.py`、`ssh_config.py`、`ssh_connections.py` 解析 SSH 配置并生成连接参数；
`auxiliary_proxy.py` 转发 Terminal、Browser 与附加资源请求，`proxy_upstream.py` 转发上游 Gateway 请求；
`device_connections.py` 管理设备连接 API；`diagnostics.py` 汇集 Gateway 诊断信息；`runtime_proof.py`
负责运行配置应用与健康证明；`service_types.py`、`workspace_ids.py` 定义共享服务类型和工作区标识；
`ui_settings.py` 合并 UI 设置。不要仅因文件变长就按语法类型拆分；应先找出独立的数据 owner 和调用边界。

# 可修改内容

- Gateway FastAPI 入口
- 工作区目标注册、激活状态、完整重连来源信息的持久化和状态检查
- 本机后端进程与 SSH 隧道进程管理
- HTTP/SSE 代理逻辑

# 不可修改内容

- 不要在 Gateway 中实现 Agent 业务逻辑。
- 不要把具体会话、消息、工具执行规则复制到 Gateway。
- 不要让 Gateway 直接读写被代理工作区的 `.boxteam` 业务数据。
- 不要把 Gateway 注册表、激活状态、SSH 隧道日志或 Web 展示状态存入默认工作区。

# 规范

- Gateway 只做路由、目标生命周期和透明转发。
- Gateway 自有控制面持久化数据位于 `${BOXTEAM_HOME:-~/.boxteams}/state/gateway/`；产品诊断日志按共享合同归 `${BOXTEAM_HOME:-~/.boxteams}/state/logs/`。
- `workspaces.json` 必须保存重启后重建目标所需的来源信息；SSH 工作区须保存主机、端口、用户、远端路径和私钥路径等重连参数，临时 `backend_url` 不能作为重连权威来源。
- 注册表写入必须原子替换并限制为当前用户可读写；私钥文件内容不得复制进注册表。
- 恢复目标失败时应保留明确的 `connection_error` 供 API 和前端观察，不得伪装为已连接。
- 失败时直接抛出明确错误，不要静默切换到其它工作区。
- 新增子目录必须补充自己的 `AGENTS.md`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
