# 目录用途

`app/gateway/registry/` 是 Gateway 工作区注册表的同名子包（facade + 七个垂直 mixin），
由原单文件 `app/gateway/registry.py` 物理下线后拆入，导入路径 `app.gateway.registry` 保持不变。

- `core.py`：顶层常量/数据类（`WorkspaceTarget`/`WorkspaceRouteLease`/
  `GatewayRegistryBatchHandle` 等）与 `RegistryCoreMixin`（`__init__` 与核心属性）。
- `persistence.py`：registry 快照持久化（`_load`/`_save`）与 commit observer。
- `crud.py`：目标增删改、排序、激活、重命名与父子关系。
- `routes.py`：路由租约、引用计数与代理目标解析。
- `runtime.py`：托管运行时生命周期与 runtime/workspace/remote generation。
- `remote.py`：远端 Gateway 连接与 retired runtime 清理。
- `projection.py`：remote projection cursor、snapshot 与 batch。
- `dtos.py`：`list_dtos` 工作区列表投影。
- `__init__.py`：facade，组装 `GatewayWorkspaceRegistry` 并再导出全部原顶层符号。

# 可修改内容

- 各垂直 mixin 的实现模块与 facade 的再导出清单。
- 新增子目录时必须补充自己的 `AGENTS.md`。

# 不可修改内容

- 修改公开契约或下线符号前，必须全仓迁齐直接、间接调用方与测试；不能保留悬空导入，
  也不能为旧聚合入口新增再导出兼容层。已有合法契约保持一致，已授权的新合同按对应 OpenSpec 实施。
- 不得把注册表写入默认工作区或任意工作区的 `.boxteam/`。
- 不得引入 `__file__`/`parents` 向上推导仓库根的路径解析；路径一律基于显式传入的
  存储路径。

# 规范

- 允许已授权的注册表与受管 runtime 生命周期闭包改动；旧“只做纯搬迁”的阶段限制不适用于后续实施。
- 注册表保有控制面身份、成员关系与路由；受管 Backend 进程 handle 和 generation 只归唯一 runtime owner，成员不复制 handle，不能按 URL 猜共享关系。
- 声称纯搬迁时仍须逐行核验语义；实际合同变更要迁齐调用方，不能以搬迁名义夹带行为变化。
- 新增模块必须保持每文件 ≤800 行。
- 注册表写入必须原子替换并限制为当前用户可读写。
- 失败直接抛出明确错误，不得静默切换到其它工作区。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
