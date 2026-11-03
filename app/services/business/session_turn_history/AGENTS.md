# 目录用途

`session_turn_history/` 负责把会话语义事件投影为完整 Job Turn，并编排有界 bootstrap、分页、详情与懒迁移。

`workspace_search.py` 在原 catalog／SessionThread 归属内编排有界消息正文查询，所有 Thread 共用原 canonical reader 的正文预算，不扫描目录或创建第二份正文。

工作区发现续点由同一`WorkspaceMessageSearchCursorError`保留形态、归属、查询、类别、目录版本及位置原因；Thread发布版本与Thread内历史冲突仍消费各自原owner异常，不合并为通用读取失败。

## 可修改内容

- 可以修改 Turn 语义投影规则、读取服务和显式迁移编排。
- 可以补充 steering 合并、重启恢复和迁移竞态的业务测试。

## 不可修改内容

- 不要直接实现文件路径、原子写入或 operation log 格式。
- 不要把前端渲染状态或 Gateway 路由逻辑放到这里。

## 规范

- 只通过 `app/abstractions/` 的协议依赖持久化和事件源。
- 语义事件缺少可靠 Job 起点时不得猜测生成 Turn。
- 迁移必须有界启动、显式发布，并在事件水位变化时保留权威投影。
