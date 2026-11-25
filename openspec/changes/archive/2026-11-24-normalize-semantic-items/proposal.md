## Why

当前 checkpoint 写入只对带工具调用的消息拆分语义 Item，普通结构化 assistant 回复可能仍把思考、文本和媒体整包保存，历史与前端再展开 Part。由此出现 Item／Part 两套身份、阅读锚点依赖 Part、SQLite 新增 `response_outline_json` 来索引二次拆分结果，增加了恢复、分页、迁移和实时收敛的复杂度。

## What Changes

- 统一 canonical 写入的语义粒度：独立思考、输出内容块、单次工具调用和单次工具结果使用独立 Item，不以是否存在工具调用决定拆分规则。
- 复用消息组及来源信息还原上游消息；保留结构化 payload 表达单项内容的属性、受保护载荷和不可解释扩展，不按 JSON 字段、Markdown 段落或流式 chunk 机械拆分。
- 单个 tool_call Item 只声明一次调用；使用 `tool_calls` 数组时恰含一个有效调用，完整数组仅在原消息协议重建时恢复。同批声明严格限定于同一条原模型消息，不能只凭相同 ModelCall 或前端活动组合批；声明与实际执行分别沿原消息组、invocation／attempt owner 表达，实时和历史默认摘要均可取得必要的安全元数据。不将数组顺序解释为串行，不将同批解释为并行，完成事件镜像延迟也不算实际执行重叠。原结果 marker 独立于时序投影，无执行摘要也保留已知失败／取消／未知结果；跨页关联限本页工具及所选 view 的可见事实，不读取整轮调用或结果正文。
- **BREAKING**：历史内容与持久阅读锚点以 `item_id` 定位，移除通用 `part_id`、`part_ordinal`、组首 Part 坐标及 Part 级分页依赖；Provider 自身的 block／part 身份只在协议映射和流式来源关联边界保留。
- 统一实时／历史的 Item 内容投影；保留临时 draft 身份与实际提交关系，不提前伪造持久 Item。finalization 选择原有消息组中真实输出 Item，不为最终展示再创建一条聚合文本事实。
- 删除仅为“一 Item 再展开多个 Part”引入的轮廓字段和算法，轻量历史摘要复用现有 Item 投影；保留有真实查询用途的安全摘要与按需正文加载。
- **BREAKING**：按用户确认退出未发布的 v1/v2 离线迁移支持，物理删除旧转换及专用入口；正常运行只接受 canonical format3，旧数据明确拒绝且保留原件，不自动转换或清理。
- 各客户端独立保存折叠状态和阅读位置；Item 身份共享不意味着 Web、Electron、移动端或不同浏览器配置共享游标。
- 执行关系仅描述所关联工具调用；调用返回后台任务／资源引用，不等于后台对象已经结束或成功，不将后台生命周期混入调用计时与结果。
- 逐条迁齐正文查询、搜索定位、URI、fork／rewind／replay、压缩、统计和前端消费者，完成后物理删除旧链路，不保留运行时双格式 reader 或 Part 别名。

## Capabilities

### New Capabilities

- 无；在现有 Item、历史和客户端视图 owner 内统一实现。

### Modified Capabilities

- `rollout-checkpoint-storage`：当前格式3的存储闸门、旧迁移退出及原 Item 索引与事务边界。
- `itemized-rollout-context`：统一语义拆分、消息组还原、Item 操作边界及 final 输出归属。
- `checkpoint-history-loading`：历史／实时使用 Item 内容投影，移除 Part 身份及轮廓索引依赖。
- `session-turn-history`：基于 Item 查询、统计与投影正文，保持 final、工具关联与保护语义。
- `user-scoped-web-view`：客户端独立的 Item 阅读锚点、折叠和取消／显式定位规则。
- `conversation-process-presentation`：承接前置变更新增的能力，将三层过程中的来源、final 和分组定位迁为 Item。该 capability 的基线位于前置变更，归档顺序见下文。

## Impact

涉及 `app/domain/itemized`、checkpoint codec、canonical／Provider 映射、rollout catalog／SQLite、history DTO／protobuf 与生成代码、Web 实时 reducer／timeline／阅读恢复及 Workbench Opener。Gateway 仍只传输和路由，不新增拆分或阅读状态 owner；沿用现有实时通道。

本变更承接已实现但尚未归档的 `unify-conversation-process-navigation`：保留三层折叠、统一展示节点入口、稳定 Item／过程／上下文来源、按需加载和延迟预算；替代其中 Part 级坐标、复合 Item 展开、`response_outline_json` 及相关分页规则。前置变更先归档，本变更再归档；实施前按任务清单核对双方受影响条款，不同时实施两套方案。

格式升级由原 owner 登记真实变更，来源按可核验发布基线或用户另外授权确定。当前 dev 的 v29→v30 授权不自动授权本次改写 canonical 历史。
