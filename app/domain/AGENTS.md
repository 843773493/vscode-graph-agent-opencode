# 目录用途

`app/domain/` 存放不依赖 I/O、Provider、LangChain 或编排流程的业务领域值对象与规则。

`background_work.py` 定义精确 Thread 后台任务描述、句柄、步骤、状态与邮箱消息／消费身份；只持有纯数据及序列化。handler 调用端口归 app/abstractions/background_task_registry.py，执行和恢复归 app/services/orchestration/background_work/，控制库直接消费这些领域值，不反向导入运行时。

# 可修改内容

- 可以新增纯领域类型、枚举、不可变值对象和确定性校验。
- 可以新增不读取文件、数据库或网络的领域序列化规则。

# 不可修改内容

- 不得访问 SQLite、JSONL、会话路径、Provider、LangChain 或 Agent runtime。
- 不得实现迁移输入读取、存储写入、请求投影或流程编排。

# 规范

- 领域对象必须保持不可变、可跨进程恢复并显式暴露非法状态。
- v2 itemized 类型是生产领域事实；legacy 只允许由 infrastructure migration 入口消费。
