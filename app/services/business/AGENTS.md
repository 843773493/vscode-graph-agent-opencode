# 目录用途

`app/services/business/` 放真正的业务服务实现，主要负责状态管理、业务规则、领域动作和对外提供的业务能力。

## 按能力查找

下面按业务流转提供导航。树中的文件名相对当前目录；现有子目录仍以目录内的 `AGENTS.md` 为职责边界。

```text
业务服务
├── Agent消息与用户内容
│   ├── agent_service.py
│   ├── message_service.py / message_display.py
│   └── user_content_builder.py
├── 会话上下文资源与查询
│   ├── session_context_resource.py              身份、URI、游标与预算基础
│   ├── session_context_projection.py            读取和搜索结果投影
│   ├── session_context_query_service.py         工作区内查询入口
│   ├── session_context_assembly_query_service.py Assembly读取
│   ├── gateway_context_query_service.py         Gateway侧聚合查询
│   ├── session_context_fork_service.py          Fork来源与转移
│   └── context_compaction_service.py            上下文压缩
├── Turn历史与检查点
│   ├── session_turn_history/                    Job Turn读取和可见页投影
│   ├── session_turn_replay_service.py           Turn回放
│   ├── reasoning_checkpoint_service.py          推理检查点
│   └── system_reminder_checkpoint_service.py    系统提醒检查点
├── 会话状态与资源
│   ├── session_service.py / session_target_resolver.py
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
│   ├── workspace_resources/     工作区文件资源
│   └── resource_derivations/    资源派生模型
├── 持久任务
│   └── persistent_task_service.py  工作区任务管理、目标和 revision 校验
├── 会话执行与通信
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
- 冷读在 topology shared 下取得 Session shared guard 并确认 catalog active 后立即释放 topology；Session guard 覆盖全部读取句柄。child thread 列表及后续团队/运行态投影由 API 持有同一次读准入，内部查询不嵌套取得 topology。
- 优先通过协议隔离依赖，避免循环引用。
