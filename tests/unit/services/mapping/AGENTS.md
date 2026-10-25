# tests/unit/services/mapping

## 目录用途

存放纯映射与结构转换逻辑的单元测试。

## 按被测转换查找

```text
mapping tests/
├── 对话内容与Turn
│   ├── test_agent_content_mapper.py
│   ├── test_user_message_content_projection.py
│   └── test_turn_response_parts.py
├── 运行事件
│   ├── test_observation_event_mapper.py
│   └── test_trace_event_mapper.py
└── itemized投影
    ├── itemized/test_selection_role_projection.py
    ├── itemized/test_carrier_dedup_tool_result.py
    ├── itemized/test_message_reasoning_merge.py 未知 model-call scope 的工具ID匹配
    ├── itemized/test_history_time_projection.py
    ├── itemized/test_root_placement_projection.py
    ├── test_history_turn_tool_identity.py      根目录测试，覆盖history_turns投影
    └── test_resource_activation_projection.py  根目录测试，覆盖sealed resource ref
```

测试文件按领域和映射入口定位；同一测试只放在一个职责位置。新增用例沿生产映射的输入事实和输出协议归类，避免按测试技巧或调用者重复建树。

## 可修改内容

- 新增、修改、删除 mapper 单元测试。
- 为输入输出结构转换补充边界用例。

## 不可修改内容

- 不要测试业务流程、持久化或外部系统调用。
- 不要依赖真实 Agent 或 LLM。

## 规范

- 测试应聚焦输入到输出的确定性映射。
- 映射失败或不支持的输入要有明确断言。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
