# 目录用途

`app/services/infrastructure/node_debug/session/` 承载 Node Debug 会话的基础设施实现：会话存储读写、会话调试状态、会话准入判定、调试动作分发与工具动作记录、启动/重启与关停/排空入口链路、只读状态读取、动作快照、线程所有权解析以及方案 fork 预发布边界。

## 职责树与数据流

目录在物理上保持扁平；按职责查找时使用下面的逻辑树：

```text
session/
├── owner 与写入准入
│   ├── thread_owner.py          规范化 Session/Thread 身份并解析 catalog owner
│   └── session_admission.py     会话级变更准入
├── 会话持久状态
│   ├── session_store.py         manifest、launch claim、方案文件与删除前 catalog owner 枚举
│   ├── session_state.py         待安装断点、动作历史和运行态 manifest 更新
│   └── snapshot.py              有界动作记录与 Node Debug 状态快照
├── 服务入口 mixin
│   ├── action_dispatch.py       apply_action 调试动作分发
│   ├── tool_actions.py          record_tool_action 工具动作记录
│   ├── launch_entry.py          start/restart 启动入口
│   ├── state_reads.py           get_state/get_variables 只读入口
│   └── lifecycle_entry.py       close/drain_session 关停入口
└── Fork 预发布
    └── fork.py                  source 快照捕获与 target 方案校验
```

常规 mutation 从 `NodeDebugService` 入口取得 owner 准入，再更新会话状态或 store；只读入口从 owner 读取状态并组装快照。Fork 流程独立冻结 source 方案 bytes，再在 target 发布前验证路径与 launch profile，不在此目录发布 Session。

# 可修改内容

- 可以维护会话 manifest/方案/claim 持久化、调试快照组装、线程归属解析和 fork 源捕获与目标校验。
- 可以维护 `NodeDebugService` 的方法族 mixin 垂直链路：`action_dispatch.py`（`apply_action` 调试动作分发）、`tool_actions.py`（`record_tool_action` 与 `_TOOL_ACTION_SOURCES` 工具动作时间线）、`launch_entry.py`（`start`/`restart` 启动入口与编排器构造）、`state_reads.py`（`get_state`/`get_variables` 只读入口）、`lifecycle_entry.py`（`close`/`drain_session` 关停入口）。这些 mixin 只声明方法，宿主字段与协作者由 `service.py` 装配。
- 可以维护本子包内模块之间的导入路径和实现细节。

# 不可修改内容

- 不得实现 API 路由、Agent 编排或进程生命周期控制。
- 入口 mixin 不得自建 owner 准入或 per-owner 临界区托管，必须复用宿主已有的 `_admit_mutation`、`_owner_lock`、`_runtimes_lock` 等能力；也不得反向依赖顶层 `service.py`。
- 不得保留旧顶层模块路径、re-export wrapper 或兼容导入别名。
- 不得拼接固定的会话/线程物理目录，必须通过统一会话路径解析器定位。
- 不得静默吞掉会话持久化或准入错误，也不得返回虚假的会话状态。

# 规范

- 文件按单一会话职责命名，只依赖会话领域模块、配置注册表与上层 schema。
- 删除前的 main／child 枚举只从权威 catalog 取得，连接打开、查询、关闭和所有归属校验在同一受保护 worker 中结算；原 Session 删除 gate 覆盖整个等待，不扫描目录或在读取未结束时推进停止、fence及物理隔离。
- 删除排空的最终运行投影写盘及claim核验同样由原worker结算；进程、持久投影和claim全部核实后才从原runtime集合移除该owner，失败或取消不得提前丢失原记录，后续关闭不能再定位已经删除的Session。
- 服务关闭先封锁新准入，Session gate 等待返回后须再次核准入；已进入的操作在 gate 退出后才结束登记。按 owner 独立等待原操作，再在原 owner 锁中读取最终 runtime 并停止；重叠关闭复用同一任务，观察超时或取消不取消原任务。失败显式重试，成功项不重放，待核实终态必须报错；每个等待者保留自己开始观察的任务及原错误。
- 需要跨子包能力时通过明确的绝对模块路径导入，不反向依赖 `service.py`。
- 失败必须直接抛出详细错误；修改本目录后运行 `uv run ruff check` 和对应 Node Debug 专项测试。
- 代码注释使用中文，公开行为变更必须同步更新调用方和测试，不得建立双轨实现。

