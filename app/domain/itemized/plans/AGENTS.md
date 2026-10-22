# 目录用途

`plans/` 持有 request-only 贡献与请求计划的纯领域合同。

# 可修改内容

- `contributions.py` 维护贡献本体、来源绑定、registry binding 清单与显式 selection 解析。
- `request.py` 维护计划 owner、顺序和封存校验以及计划哈希入口。

# 不可修改内容

- 不得访问 SQLite、JSONL、路径、Provider 或 LangChain。
- 不得维护 registry 序号分配、第二套贡献解析或旧导入路径垫片。

# 规范

- 贡献类型只在 `contributions.py` 定义；计划和消费者直接依赖该 owner。
- 保持既有不可变快照、typed source binding、hash 与错误语义。
- 持久字段由 `serde/` 唯一恢复，不按当前资源补写历史。
