# 目录用途

`session_turn_history/` 基于已提交的 rollout 编排精确 Thread 的 bootstrap、历史分页、详情及消息／工具结果查询。活动 Job 摘要来自 JobService；Session Trace 只提供实时流接续游标，不作为对话历史正文来源。

`workspace_search.py` 在原 catalog／SessionThread 归属内编排有界消息正文查询，所有 Thread 共用原 canonical reader 的正文预算，不扫描目录或创建第二份正文。

工作区发现续点由同一`WorkspaceMessageSearchCursorError`保留形态、归属、查询、类别、目录版本及位置原因；Thread发布版本与Thread内历史冲突仍消费各自原owner异常，不合并为通用读取失败。

## 可修改内容

- 可以修改 rollout 历史读取、分页与活动 Job 摘要的组合。
- 可以补充精确 Thread、历史分页、活动 Job 和实时接续的业务测试。

## 不可修改内容

- 不要直接实现文件路径、原子写入或 operation log 格式。
- 不要把前端渲染状态或 Gateway 路由逻辑放到这里。

## 规范

- 历史正文通过 RolloutCheckpointSaver 的统一历史入口读取，不从 Trace 重建 Turn 或维护第二套历史投影。
- 在同一次 Session 读准入内解析 Thread 并读取历史；活动 Job 状态继续由 JobService 提供。
- bootstrap 的 Trace 游标沿用 Session／Thread 绑定，只服务于后续事件流。
