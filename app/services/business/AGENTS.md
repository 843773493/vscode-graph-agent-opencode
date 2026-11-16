# 目录用途

`app/services/business/` 放真正的业务服务实现，主要负责状态管理、业务规则、领域动作和对外提供的业务能力。

## 按能力查找

下面按业务流转提供导航。树中的文件名相对当前目录；现有子目录仍以目录内的 `AGENTS.md` 为职责边界。

```text
业务服务
├── Agent消息与用户内容
│   ├── agent_service.py
│   ├── message_service.py         消息准入、checkpoint 读取和消息查询
│   ├── message_display.py         内部消息的可见性与公开内容策略
│   └── user_content_builder.py
├── 会话上下文资源与查询
│   ├── session_context_query/                  读取、搜索和冻结查询投影
│   │   ├── workspace.py / gateway.py           工作区查询与 Gateway inventory 查询
│   │   ├── assembly.py                         已封存 Assembly 读取
│   │   ├── resources.py                        资源身份、URI 与视图准入
│   │   └── projection.py / pagination.py       内容投影与统一游标、字符预算、分片
│   ├── session_context_fork_service.py          Fork来源与转移
│   └── context_compaction_service.py            上下文压缩
├── Turn历史与检查点
│   ├── session_turn_history/                    Job Turn读取和可见页投影
│   ├── reasoning_checkpoint_service.py          推理检查点
│   └── system_reminder_checkpoint_service.py    系统提醒检查点
├── 会话状态与资源
│   ├── session_service.py
│   ├── session_information_service.py / session_goal_service.py
│   ├── session_interrupt_service.py
│   ├── session_changes_logic.py / session_changes_service.py
│   ├── session_skill_tracking_service.py
│   ├── session_resource_service.py
│   ├── session_resource_actions.py
│   ├── session_resource_providers.py
│   ├── session_resource_registry.py
│   ├── browser_resources/       Browser资源归属
│   ├── terminal_resources/      Terminal资源归属
│   ├── workspace_resources/     Terminal／Browser 共用的准入、保留、关联与 lease
│   └── resource_derivations/    资源派生模型
├── 持久任务
│   ├── persistent_task_service.py  工作区任务管理、目标和 revision 校验
│   └── persistent_tasks/goal.py    Goal 目标、预算记账与内部提示的纯应用规则
├── 会话执行与通信
│   ├── search/                  工作区具名搜索 Provider 与安全结算
│   ├── session_generation/      生成入口和结果回执
│   ├── session_navigation/      导航操作及目录状态
│   ├── communication/           会话间通信
│   └── job/                     Job状态和执行调度
└── 团队能力
    └── team/                    团队协作业务
```

新增能力优先放入已有职责子目录；若一个领域仍只能放在本目录根层，先确认它没有可复用的现有归属。物理迁移时要全量迁移所有生产和测试导入，删除旧路径，不留转发模块；同一批改动同步更新本树。

## 可修改内容

- 可以新增、修改、删除业务服务实现文件。
- 可以调整业务服务内部依赖，但应继续依赖 `app/abstractions/` 中的协议，而不是直接耦合容器或 API 层。

## 不可修改内容

- 不要把流程编排、事件推送、运行时组装放进这里。
- 不要把纯基础设施能力放进这里。

## 规范

- 保持每个文件单一职责。
- 出现业务失败时直接抛出明确错误，不要静默降级。
- Agent 列表只校验 Provider 本地配置及必要凭据，必须在线程中执行并保持逐 Provider 顺序及配置错误原文；SDK 初始化和构造校验留在执行入口。凭据可独立更新，不得按配置 revision 缓存可用状态，不阻塞工作区事件循环的健康检查。
- Session Changes 列表只生成摘要，单次编辑复用已保存 patch；同一文件多次编辑仍比较首尾快照，保留相互抵消的净变化。列表按 Turn 一次分组，All 与 Turn 的相同编辑组只计算一次，单次请求仅复用记录和行数摘要，不缓存快照正文或派生 diff。索引和正文 reader 在同一次会话准入内复用已解析的真实根，逐文件仍检查 edit_id 与路径边界，不跨请求缓存路径或正文。涉及 CR/CRLF 的 patch 沿用快照读取的统一换行语义；未消费快照的缺失在详情读取时报错，不为列表重读全部正文或新建持久化缓存。
- Session Changes 的编辑记录、列表净变化和详情共用 `build_unified_diff`，匹配使用 cdifflib，保留 difflib 的 autojunk、三行上下文、行号与原始换行拼接格式；不修改标准库全局 matcher，不为 diff 新增持久化或正文缓存。
- 冷读在 topology shared 下取得 Session shared guard 并确认 catalog active 后立即释放 topology；Session guard 覆盖全部读取句柄。child thread 列表及后续团队/运行态投影由 API 持有同一次读准入，内部查询不嵌套取得 topology。
- 优先通过协议隔离依赖，避免循环引用。
- 上下文目录搜索只消费 catalog，不加载消息历史；目录命中 revision 只绑定目录数据，消息展开从 read_context 取得消息修订。混合来源搜索的续页同时绑定已选来源；effective_context 命中仍可用 record_index 和内容 revision 展开。正文搜索统一使用 regex 引擎，批量匹配在线程中执行；读取和匹配共用最长 15 秒的搜索期限，上游期限只能缩短，取消须等待 worker 结算后再释放准入。
