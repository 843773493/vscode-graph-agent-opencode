## Why

当前过程展示在实时与历史之间采用不同规则，中间说明、最终回答和工具详情的归类不一致；阅读位置仍以 Turn 内像素偏移保存，折叠、宽度变化以及没有 `turn_id` 的内部执行都会破坏恢复。采用 Kunlun-Code-CYX 的三层折叠 v1，并将展示、稳定 Item 定位和按需读取统一设计，解决过程堆积、位置跳动及答案与摘要先后闪现的问题。

## What Changes

- 统一“执行过程 → 连续思考／工具组 → 单项详情”三层展示；运行时过程默认展开、终态自动折叠，最终回复独立展示，中间文本切分活动组。
- 普通 Turn 与无 Turn 的内部 execution 共用展示机制，保持真实业务身份、canonical 顺序、工具回执与原失败语义。
- transcript 内统一轻量展示节点合同：消息、折叠标题、上下文虚线及独立数据卡片使用稳定来源与展示部位标识；装饰、间距和临时加载行不另建持久锚点。不新增运行时注册服务或展示节点数据库。
- **BREAKING**：阅读位置从 `turn_anchor + scroll_offset` 改为稳定展示节点、节点内位置和视口相对位置，节点以 source_ref＋slot 标识，关联真实 Item／Part 或原上下文等来源；按完整 Gateway／工作区／Session／Thread 隔离，折叠状态先于位置恢复。
- 搜索、引用、重复跳转与恢复共用原历史 owner 和虚拟列表入口；扩展同一 history 请求支持按 Item 加载附近窗口，不另建定位服务或消息存储。
- 复用原 history 的过程级活动轮廓续页与最多四项 Item 正文回补，完整投影与片段沿同一 canonical reducer 按覆盖范围应用；按层加载活动轮廓与正文，未展开的工具结果及思考不全量读取、解析或挂载；保留原字节、分页和交互延迟预算。
- 在现有 transcript 内统一阅读位置控制器，区分被折叠、未加载、虚拟卸载、权威确认不可用和加载故障；原业务 owner 继续负责存在性与加载，故障不走相邻位置的成功替代。迁移仅归原视图记录 owner，新增节点和普通布局变化不自动触发迁移。
- 删除实时／历史分开的摘要实现、末段 Markdown 推断 final、Turn-only 阅读恢复及重复折叠状态；仅提供三层 v1。

## Capabilities

### New Capabilities
- `conversation-process-presentation`：三层结构、稳定分组、可扩展展示节点合同、运行到终态的折叠行为、摘要布局、焦点与必要操作入口。

### Modified Capabilities
- `session-turn-history`：按层活动详情、Item／Execution 有界定位，替换主 Thread 专用位置与 Turn-only 跳转限制。
- `user-scoped-web-view`：展示节点锚点、统一定位结果、视图格式迁移边界、逐 Thread 折叠记忆、恢复顺序和同屏编辑器的展示隔离。
- `progressive-turn-rendering`：虚拟列表按可见内容保持位置，三层按需水合与真实浏览器验收。
- `checkpoint-history-loading`：将 Turn-only 定位扩为稳定 Item／Part 位置，并让原复合 cursor 冻结 Item around 的查询坐标，保留两路分页和单 snapshot 约束。

## Impact

- React 会话组件与投影：`src/clients/web/src/components/chat/`、`components/panels/ChatPanel.tsx`、`state/timeline/`，以及既有 internal execution canonical 合并入口。
- 视图与定位：`state/sessionViewState.ts`、`hooks/session/browserSessionViews.ts`、`hooks/session/useSessionViewState.ts`、`api/session/sessionTurnHistory.ts`、既有虚拟滚动与导航 hooks。
- 历史 reader 与协议：`app/services/infrastructure/rollout_history/`、`app/services/mapping/itemized/history_turns/`、`app/schemas/internal_v2/turn.py`、`proto/boxteam/workspace/v2/public.proto` 及其全部生成消费者；扩展原查询，不新增 Gateway 业务逻辑。
- 使用既有 IndexedDB 视图存储，不新增数据库、原生 IPC、外部依赖或常驻连接；不改变模型、工具、排队或持续任务调度。
- 与 `unify-failure-reporting` 共用既定 failure metadata，后者尚未完成的验收不由本变更冒充完成。
