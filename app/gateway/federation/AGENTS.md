# 目录用途

`app/gateway/federation/` 存放 Gateway 联邦控制面：一个中心 hub 与其直接 spoke
之间通过 SSH `-L` loopback 建立的长期全双工 WebSocket 对等 RPC channel，以及
绑定 origin/transit path/audience/capability/target/nonce 的 discovery/operation
grant 与 `permissions.federation` 策略快照。

## 可修改内容

- `ws://.../api/gateway/federation/channel` 的握手、心跳、断线语义与 RPC 分发。
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
