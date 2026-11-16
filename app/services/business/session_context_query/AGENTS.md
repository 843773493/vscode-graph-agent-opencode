# 目录用途

拥有会话上下文资源的读取、搜索和冻结投影；不创建会话、不转移上下文，也不执行压缩。

- `workspace.py`：`SessionContextQueryService`，工作区内读取与搜索，按请求固定来源和修订。
- `gateway.py`：`GatewayContextQueryService`，Gateway inventory 的查询投影及工作区查询传输。
- `assembly.py`：`SessionContextAssemblyQueryService`，已封存 Assembly 的目录和内容投影。
- `resources.py`：资源身份、URI 解析和读取视图准入。
- `projection.py`：消息记录、会话目录及冻结读取快照的内容投影。
- `pagination.py`：游标编码解码、修订与位置校验、响应字符预算和读取分片。

# 可修改内容

- 可以调整上述查询与投影；调用方直接导入实际 owner 模块。
- 普通读取和 durable observation 共用冻结读取快照及分页器。

# 不可修改内容

- 不实现上下文 Fork／Transfer、消息准入、checkpoint 持久化或第二套历史。
- 不把目录查询变成消息历史扫描，不另建 Gateway 联邦搜索调度。
- 不保留旧路径转导出或 `CursorCodec` 静态方法包装类。

# 规范

- 资源定位归 `resources.py`；分页、字符预算与游标绑定归 `pagination.py`，不能在查询入口各自实现。
- `projection.py` 生成可见内容，分页器只消费 DTO，不反向读取消息或依赖查询 service。
- 保持现有游标信封、错误归因、码点位置、预算及序列化结果；全仓迁齐动态引用和测试后删除旧入口。
