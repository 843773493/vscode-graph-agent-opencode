# 目录用途

验证附件经产品消息入口生成 canonical 内容、模型请求与恢复记录的跨模块合同。

# 可修改内容

附件发送、预览失败、历史冷读、工作副本释放/重建及其集成测试专用故障注入。

`test_thread_isolation.py` 从真实消息及 fork 生成 main/child 历史，分别排队并完成工具循环，以独立 SQLite/JSONL、请求及重启历史核对隔离；外部模型复用 model_stream，工作区读取不应增加 Thread 保活关联。

`test_frozen_history.py` 复用原 Gateway/Backend 与 fork 监督器，经上传、工具、文件编辑、full copy、正式副本执行和重启，验证修改结果及 Session 删除后仍保留；副本通过原工具显式删除结果回到基线后才释放工作副本。源与副本共用精确请求重建断言，核对历史身份、原字节/hash和副本续聊；不手填关联、待处理或 canonical，不代替 Web、Windows及硬退出验收。

# 不可修改内容

不预先写入 canonical 历史，不拼装独立 SSE 协议，不修改 fixture 源工作区。

# 规范

复用既有生产容器和 model_stream 装具；明确故障注入边界，正式输出遵守测试路径镜像规则。

`test_file_access_lifecycle.py` 从真实消息和图卸载制造在途读/列表/媒体/下载与最后解绑竞争，核对同 ID 排队交接、旧分片碰撞的不同 ID 隔离、ASGI 完成/断连后的原 holder 结清，以及历史观察恢复不物化 released 目录。原文件 operation 持附件 holder 等待观察锁时，普通工作区列表不得反向等待该 holder；所有屏障在 finally 释放。

同文件的 watch 场景从真实上传/卸载进入公开 SSE 适配器，核 locator 初始化不持正文 holder、最后 detach 可完成结清、released 根及目录形态、原 URI 重建事件、其它附件/私有状态过滤；原生 Shell 子进程以附件根为 cwd 将 derived 替换为文件后，公开 watch 必须按实际类型拒绝，仅实际缺失的固定布局保留虚拟目录语义，此定向场景不复验 PTY。原 operation 事件经真实 API 回放并区分显式订阅与私有事件。reveal 场景仅替换系统启动器派发边界，核解析/正文原 holder、卸载竞争及原件重建，不宣称已验证外部 GUI 窗口持续使用期。

`test_process_retention_restart.py`由正式模型工具调用启动附件目录中的父子进程；附件外门控和进程回执保证工具返回、Thread卸载及两次Backend硬重启时目录仍未修改，随后才允许真实命令首次写入处理结果。原保活suite只运行`live`，停止恢复suite在真实派发前或正式回执返回后硬退出Backend。核对原lease、无自动重放、真实进程终态，以及多次重启后处理结果仍保留，不由测试终止处理进程或结算占用。同文件套件在同一机器串行；测试节点名保持简短以免Windows装具路径超限，不减少业务步骤或断言。

`test_startup_operation_restart.py`经真实消息上传和管理创建生成目录与原operation；复用Terminal启动故障装具及原SHELL配置启动真实父子进程，只丢停止证明。Backend硬重启及显式工作副本检查必须保留原unknown和精确占用，原管理停止核实后才结清并释放无修改目录；不手填账本或原生身份，不代替无身份接管、Windows或Web验收。
