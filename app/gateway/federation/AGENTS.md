# 目录用途

`app/gateway/federation/` 存放 Gateway 联邦控制面：一个中心 hub 与其直接 spoke
之间通过 SSH `-L` loopback 建立的长期全双工 WebSocket 对等 RPC channel，以及
绑定 origin/transit path/audience/capability/target/nonce 的 discovery/operation
grant 与 `permissions.federation` 策略快照。

## 文件职责树

```text
federation/
├── __init__.py                    hub/spoke 组合与 Gateway 管理调用
├── router.py → endpoint.py        FastAPI 接入、WebSocket 握手与请求分发入口
├── channel.py + transport.py      双向 channel 状态机和服务端/拨号端传输适配
├── channel_owner.py + dialer.py   已登记 peer 的长连接、重连与 hub 主动拨号
├── rpc.py                         channel 绑定、discovery/transit、operation 执行与授权
├── protocol.py + messages.py      帧合同、载荷校验、hop/deadline 预算
├── identity.py + grants.py        peer 身份、签名授权和 grant 校验
├── policy.py                      federation capability 策略快照
├── ports.py + workspace_port.py   RPC 依赖协议及本地工作区查询/执行端口
├── skill_sources.py               通过已登记 channel 读取 Workspace Skill owner
├── store.py + schema.sql          replay registry、channel、route hint 持久化
└── errors.py                      稳定联邦错误码与异常映射
```

`rpc.py` 是共享 channel 与授权状态的协调 owner；hub/spoke 的路径差异留在同一
dispatch 流程中，纯帧、载荷、grant 和策略规则分别归上面的专用模块。不要按
“一侧一个实现”拆出重复信任逻辑，也不要把 Gateway 协议逻辑放入 Workspace 业务层。

## 可修改内容

- `ws://.../api/gateway/federation/channel` 的握手、心跳、断线语义与 RPC 分发。
- 复用现有 registry 的 SSH 隧道、凭据及 peer/channel 生命周期，在 Gateway 组合根接通 hub/spoke 目录与长期 channel；工作区请求仍透明转发给目标 backend 的业务 owner。
- grant 签发/校验、transit 上限、first-use replay registry、channel 注册表与
  短 TTL route hint。
- `permissions.federation` 默认值与原子热发布快照。

## 不可修改内容

- 不实现 Agent/Job/消息业务逻辑；不写工作区 `.boxteam` 业务数据。
- 不新增第二套 federation token 校验；凭据校验统一复用 `app/gateway/auth.py`。
- 不把 channel/route/connection locator 写入 link、`GlobalThreadAddress` 或任何
  业务幂等键。
- 不在 hub 保存业务消息正文。

## 规范

- 身份、完整性、audience/path、防重放、target 解析与幂等不可关闭；失败一律抛
  携带稳定错误码的 `FederationError`，绝不静默降级或返回虚假默认值。
- 联邦持久状态只写 Gateway 控制面目录；replay registry 跨重启保留有效窗口。
- 只允许一次 `B → A → C` transit：`max_transit_gateways=1`、`max_gateway_hops=2`。
- 新增子目录必须补充自己的 `AGENTS.md`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
