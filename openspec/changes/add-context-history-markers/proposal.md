## Why

会话时间线目前只为部分 `runtime_notice` 提供上下文标记，System Prompt 等 request-only 来源未覆盖。用户偶尔需要知道可见消息之间或某次模型请求前实际使用了哪些上下文，但默认历史加载不应为此读取提示正文或完整请求快照。

## What Changes

- 将既有标记扩展到实际派发请求选入的上下文文本，包括 System Prompt、工作区指引、Skill 和内部运行提示；默认仅查询 SQLite 元数据。
- 在会话时间线显示低干扰的居中虚线标记，右击选择“查看上下文内容”才读取冻结正文，并复用 Workbench 只读编辑器。
- 固定展示位置：首次 System Prompt 和首次内部用户角色注入的标记位于 transcript 最上方、第一条真实用户消息之前；打断提示在后续请求实际使用后，标记位于新用户消息之前，实时及历史一致。
- 复用现有 sealed assembly、contribution、canonical Item 和不可变内容存储，补齐精确正文引用、首次使用／版本变化定位、分页与 Fork 行为。
- 明确历史读取、实时失效通知、正文大小边界和性能验收，移除当前点击展开即读取正文及打开流程重复传输；本功能不新增保密分级、权限配置或审批，普通上下文不因默认隐藏而被禁止查看。

## Capabilities

### New Capabilities

- `context-history-markers`：上下文文本的轻量存在性标记、稳定定位、右键按需查看及端到端性能合同。

### Modified Capabilities

- `checkpoint-history-loading`：允许上下文元数据标记进入原历史页面，仍不新增 Turn、不默认返回 overlay 正文。
- `itemized-rollout-context`：明确标记对 sealed contribution／Item 的冻结引用与正文保留、定点读取职责。

## Impact

- 后端：`rollout_context/storage/catalog/context_notices.py`、assembly 封存／内容 owner、checkpoint reader、`rollout_history/reader.py`、历史 mapper、历史／内容 inspection API 与协议生成。
- 客户端：历史状态及时间线、`ContextNotice`／消息右键菜单、Workbench `itemActions` 和只读内容文档；Web 与 Electron 继续共用实现。
- Gateway 继续路由与代理，不读取工作区业务数据库。无新增轮询、聊天专用快照、假 Item 或全局索引。
- 复用既有定向测试和真实 Workbench／Gateway／Backend 装具；实现按纵向切片替换已有链路，不保留第二套标记或内容读取实现。
- 必须通过“页面新建会话→发送→运行中打断→再次发送→右键查看→刷新／重开历史”的真实集成场景，校验标记位置、冻结正文和按需读取；仅单测或预置历史截图不能替代。
