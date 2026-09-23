# 目录用途

`app/schemas/event/` 定义工作区后端 Agent 执行事件的 Pydantic 模型，是该事件模式 discriminated union 的唯一实现点。目录当前只有 `__init__.py`，内容分四段：`BaseEvent` 公共字段与身份校验、各事件的 Payload Schema、带 `type` 字面量的具体事件类，以及末尾的 `Event` union 类型。

事件的权威来源是 `app/core/job_event_bus.py` 定义的事件类型集合；`Event` union 被 `app/services/`（事件流处理、trace 记录、turn history）与 `app/api/`（jobs 事件查询）消费。

# 可修改内容

- 可以新增、调整各事件的 Payload 与事件类，并同步更新末尾的 `Event` union 成员。
- 可以补充事件字段级的 Pydantic 校验规则（如 `BaseEvent.validate_part_identity`）。

# 不可修改内容

- 不在本目录定义跨进程 / 对前端的线格式绑定：面向 Gateway 与前端的公开协议以 `app/protocol/`（`.proto` 与 `app/protocol/codecs/`）为唯一来源，本目录只提供后端内部 Pydantic 模型与 FastAPI 运行时校验。
- 不在这里实现事件落盘、投影或分发逻辑；这些属于 `app/services/`。
- 不要绕过 `Event` union 另建一套事件类型枚举或判别字段，导致双轨。

# 规范

- 每个事件类必须带 `type` 字面量字段，并通过 `BaseEvent` 继承公共字段，使 Pydantic 能按 `type` 正确判别 union 成员。
- `timestamp` 必须包含时区；带 `part_id` 语义的事件（`text_*`、`tool_call_*`）必须携带 `part_id`。
- 校验失败直接抛出明确错误，不用默认值掩盖协议不匹配。
- 新增事件类型时同步在 `app/core/job_event_bus.py` 的事件集合与消费侧（`app/services/orchestration/event_stream/`、`app/services/mapping/`）登记。
