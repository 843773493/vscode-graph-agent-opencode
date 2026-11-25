# 目录用途

`app/services/mapping/` 放纯转换逻辑，负责在不同 DTO、事件模型和内部结构之间做映射，不承载业务决策。

## 按输入与输出查找

```text
mapping/
├── 对话内容与Turn展示
│   ├── agent_content_mapper.py             Agent流式内容 → 可见响应块
│   ├── message_content.py                  checkpoint消息 → 可见DTO、附件与轻量内容
│   ├── agent_state_messages.py             原生消息或字典 → Agent State原始记录
│   ├── user_message_content_projection.py   canonical用户正文 → 展示正文与附件元数据
│   └── turn_response_items.py               canonical Rollout → 历史/live共用响应 Item
├── 会话资源
│   ├── session_resource_mapper.py           后台任务句柄、Terminal／Browser快照 → 会话资源DTO
│   └── session_catalog_search.py             公开 catalog 节点 → 后端与 Gateway 共用搜索键
├── 运行事件
│   └── trace_event_mapper.py                trace_event_to_dto：持久Trace事件 → 公开Trace DTO
├── 工具管理目录
│   └── tool_catalog.py                      tools_to_catalog_definitions：已注册工具 → 管理目录定义
└── itemized/                                selection、provider与canonical history投影
    └── history_turns/                       已提交历史消息 → Turn 详情、摘要及独立执行历史 DTO
```

树按转换边界归类，不代表新增文件层级。选择入口时先看输入事实由谁提供、输出要进入哪种协议；只有出现稳定的同类职责簇时才移动文件，并同步迁移全部导入和对应测试。

## 可修改内容

- 可以新增、修改、删除映射函数文件。
- 可以整理事件转换、观测数据转换、响应组装等纯映射逻辑。

## 不可修改内容

- 不要在这里写业务判断、流程编排或 I/O。
- 不要在这里直接访问容器、数据库或外部系统。

## 规范

- 保持函数尽量纯粹、无副作用。
- 映射失败要显式暴露，不要静默丢字段。
- 命名要能直接体现输入与输出的语义。
- 消息转换使用模块函数，由 `MessageService` 显式调用；不通过 mixin 继承隐藏纯映射方法。Agent State 保留原生内容及恢复字段，可见 DTO 仍按业务展示策略投影，二者不能相互替代。
- 会话资源转换由三个 `*_to_session_resource` 纯函数提供；provider 决定动作权限后直接调用，不在容器装配无状态 mapper 实例。
- Trace 列表、流与搜索详情统一调用 `trace_event_to_dto`；事件归属由调用方提供，转换函数保留原归属和字段校验，不持有读取状态。
- 工具目录转换不构建工具、模型或图；参数 schema 复用 `agents/model_tool_schema.py` 的模型可见投影。配置与 MCP 版本缓存归 `AgentToolCatalog`，不在 mapper 保存第二份目录；MCP 参数直接消费已发布定义，custom 来源、分组与配置描述沿原 helper。聚合在转名称字典前拒绝重名。
