# 目录用途

`app/services/mapping/` 放纯转换逻辑，负责在不同 DTO、事件模型和内部结构之间做映射，不承载业务决策。

## 按输入与输出查找

```text
mapping/
├── 对话内容与Turn展示
│   ├── agent_content_mapper.py             Agent流式内容 → 可见响应块
│   ├── message_content.py                  Message DTO、附件与Agent State → 消息投影
│   ├── user_message_content_projection.py   canonical用户正文 → 展示正文与附件元数据
│   └── turn_response_parts.py               canonical Rollout → 历史/live共用响应部件
├── 会话资源
│   └── session_resource_mapper.py           会话资源记录 → 资源DTO
├── 运行事件
│   ├── observation_event_mapper.py          业务事件 → SSE与Proto观测
│   └── trace_event_mapper.py                Trace事件 → Trace DTO
└── itemized/                                selection、provider与canonical history投影
    └── history_turns/                       已提交历史消息 → Turn content/tool/detail DTO
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
