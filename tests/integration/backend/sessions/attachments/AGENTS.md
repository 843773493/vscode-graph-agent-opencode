# 目录用途

验证附件经产品消息入口生成 canonical 内容、模型请求与恢复记录的跨模块合同。

# 可修改内容

附件发送、预览失败、历史冷读、目录回收成本测量及其集成测试专用故障注入。

`test_thread_isolation.py` 从真实消息及 fork 生成 main/child 历史，分别排队并完成工具循环，以独立 SQLite/JSONL、请求及重启历史核对隔离；外部模型复用 model_stream，工作区读取不应增加 Thread 保活关联。

# 不可修改内容

不预先写入 canonical 历史，不拼装独立 SSE 协议，不修改 fixture 源工作区。

# 规范

复用既有生产容器和 model_stream 装具；明确故障注入边界，正式输出遵守测试路径镜像规则。

目录测量使用产品消息与执行入口生成关联和期限，记录规模、SQLite 占用及查询/冷恢复/GC 原始耗时；不以固定耗时或压缩比例作正确性断言。

`test_retention_restart.py`复用现有Gateway/Backend与model_stream装具，经两次正式消息执行生成目录和runtime关联；进程外UTC时钟推进第29/58/59天，SIGKILL后原启动owner提交零关联和GC。只读SQLite与物理原件/canonical核对，不预填期限、关联或历史；正式suite为`integration-attachment-retention-restart`，240秒/4GiB。

`test_process_retention_restart.py`由正式模型工具调用启动附件目录中的父子进程；原保活suite只运行`live`，停止恢复suite在真实派发前或正式回执返回后硬退出Backend。核对原lease、无自动重放、真实进程终态和原期限GC，不由测试终止处理进程或结算占用。同文件套件在同一机器串行；测试节点名保持简短以免Windows装具路径超限，不减少业务步骤或断言。

`test_startup_operation_restart.py`经真实消息上传和管理创建生成目录与原operation；复用Terminal启动故障装具及原SHELL配置启动真实父子进程，只丢停止证明。Backend硬重启及到期查询必须保留原unknown和精确占用，原管理停止核实后才结清并回收目录；不手填账本或原生身份，不代替无身份接管、Windows或Web验收。
