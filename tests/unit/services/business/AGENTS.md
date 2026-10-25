# tests/unit/services/business

## 目录用途

存放业务服务单元测试，覆盖状态管理、领域动作和业务服务对外能力。

## 按业务职责查找

```text
tests/unit/services/business/
├── communication/        Session 间消息与接纳规则
├── job/                   Job 队列、取消、超时和服务收口
├── resource_derivations/  语义资源投影与派生
├── browser_resources/     Browser操作回执、版本和原占用保护
├── terminal_resources/    Terminal操作回执、工作区完成与受管目录占用
├── workspace_resources/   两类资源共用的授权、等待期限与生命周期
├── session_generation/    Session 创建与默认值
├── session_children/      child-thread 读模型、Team board 发布投影与创建 owner
├── session_navigation/    Session 导航目录与操作队列
├── session_turn_history/ Turn 历史读取
├── team/                  团队协作与成员状态
└── 根目录测试
    ├── Session owner 与导航
    │   ├── test_session_service*.py
    │   ├── test_session_target_resolver.py、test_session_information_service.py
    │   ├── test_session_changes_service.py、test_session_interrupt_service.py
    │   └── test_session_goal_service*.py
    ├── 上下文、分页与回放
    │   ├── test_session_context_*.py、test_internal_history_paging.py
    │   └── test_session_turn_replay_service.py
    ├── checkpoint 与提醒
    │   ├── test_*checkpoint*.py
    │   └── test_system_reminder_checkpoint_service.py
    ├── 资源 owner
    │   ├── test_browser_resource_owner.py、test_terminal_resource_owner.py
    │   └── test_session_resource_*.py
    └── Agent、Gateway 与内容
        ├── test_agent_service.py、test_gateway_context_query_service.py
        ├── test_user_content_builder.py
        └── session_service_test_support.py（共享测试装具）
```

根目录有 24 个直接测试模块，仍超过 20 个的结构审查阈值；`test_message_service_checkpoint.py`、`test_reasoning_checkpoint_service.py`、`test_session_context_query_service.py` 和 `test_session_service.py` 都超过约 800 行，应在各自行为边界稳定后优先做文件拆分审查。child-thread 的列表读模型、Team board 发布投影和创建 owner 校验已分到 `session_children/`，避免混在一个超长模块中。移动测试时检查测试矩阵、`out/tests/` 镜像路径和引用；不要只按行数切文件。

## 可修改内容

- 新增、修改、删除业务服务相关单元测试。
- 使用 fixture、fake 或临时目录隔离依赖。

## 不可修改内容

- 不要测试流程编排细节。
- 不要引入真实外部服务或网络调用。

## 规范

- 测试应快速、独立、可重复。
- 文件系统副作用必须使用 `tmp_path` 等临时资源隔离。
