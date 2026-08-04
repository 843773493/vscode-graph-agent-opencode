## 1. 后端 Turn 读取模型

- [x] 1.1 定义 Turn summary/detail、bootstrap、分页 cursor 与 stale-cursor 公共 schema，并补充 schema 测试
- [x] 1.2 在会话节点实现 append-only TurnHistoryStore、manifest/index、revision、epoch、幂等恢复和压实测试
- [x] 1.3 从 Job/message/text/tool/terminal 语义事件增量维护 Turn 投影，覆盖普通 Job、steering 合并和重启恢复
- [x] 1.4 实现有界 bootstrap、Turn summary 分页和批量 detail API，验证普通追加与 checkpoint 压缩不使 cursor 失效
- [x] 1.5 为既有会话实现显式懒迁移/重建路径，并验证损坏与旧 epoch 快速失败
- [x] 1.6 将编辑重发、重新生成和失败重试接入 Turn 投影破坏性回退，确保 checkpoint、可见 Turn 后缀和 projection epoch 一致
- [x] 1.7 为 staging 发布加入 projection epoch 与事件水位联合 CAS，并将大量 Turn 隐藏改为 header-only 更新和 detail 流式复制

## 2. 前端 Turn 状态与 Composer 解耦

- [x] 2.1 增加 Turn/bootstrap API 类型、客户端和按 workspace/session 分区的 `turn_id + revision` upsert 状态
- [x] 2.2 将会话切换改为 Composer 立即可用、bootstrap 最新 Turn 优先，并使用 generation/取消处理乱序响应
- [x] 2.3 将 Composer 状态订阅从完整 AppState 中拆分，按 scope 同步恢复草稿并加入不随 timeline delta 重渲染的测试
- [x] 2.4 将分页、SSE 和 Job 终态协调改为 Turn upsert，保留旧页并移除主聊天的无上限 Trace 恢复
- [x] 2.5 清理浏览器主时间线旧 message 分页状态和不可达分组路径，保留必要的单消息/诊断 API

## 3. 渐进 Markdown 与可视详情水合

- [x] 3.1 让 ChatPanel 以完整 Turn 渲染和分页，保持 Virtuoso 顶部视觉锚点与稳定 Turn key
- [x] 3.2 实现 latest Turn summary 先呈现、full detail 原位水合和可视区加受限 overscan 的批量详情请求
- [x] 3.3 对 Turn/Markdown 组件增加稳定 memoization 和低优先级 Markdown 增强，折叠 reasoning/tool 内容按需解析
- [x] 3.4 添加大型 Markdown、详情乱序、快速滚动与历史前插的组件/状态测试
- [x] 3.5 区分用户中断、普通取消和真实运行失败，去重同一 Turn 的 `session_interrupted`/`job_cancelled` 展示

## 4. E2E 与性能验收

- [x] 4.1 建立隔离长会话夹具，生成大量完整 Job Turn、大型 Markdown、Trace 和可触发压缩的上下文
- [x] 4.2 添加后端 E2E，验证最新 Turn bootstrap、完整 Turn 分页、详情上限、稳定 cursor、stale epoch 和压缩不失效
- [x] 4.3 添加真实浏览器 E2E，验证慢历史请求时 Composer 先可输入、最新 Turn 优先、分页不拆 Turn且终态不丢旧页
- [x] 4.4 添加 SSE/bootstrap 竞态、steering 合并、历史错误透明和主聊天不请求完整 Trace 的回归测试
- [x] 4.5 添加 replay 旧 cursor、过期 staging、destructive rebuild、全隐藏索引、header-only 内存边界和实时重复扫描回归测试

## 5. 集成与交付验收

- [x] 5.1 运行 Python 静态分析与相关 unit/integration/E2E，修复全部失败
- [x] 5.2 运行 `bun --cwd src/web run build`、前端测试与真实浏览器长会话性能验收
- [x] 5.3 审查目录结构、会话存储边界、旧代码清理和 OpenSpec 一致性，确认没有 P2 及以上问题
- [x] 5.4 由独立 subagent 复审 bootstrap、分页、详情水合、SSE、epoch 与 replay 竞态，处理发现项并完成二次确认
