# 目录用途

存放 Node Debug 进程身份探测的跨平台单元测试。

# 可修改内容

- 可以添加通过真实进程身份验证 OS 探测路径的单元测试。
- 测试依赖统一使用 pytest fixture 注入，进程必须由用例拥有并回收。

# 不可修改内容

- 不替代 `tests/unit/services/infrastructure/` 中既有的 Node Debug 状态机验收。
- 不在测试中终止未由当前用例创建的进程。

# 规范

- 新增用例应覆盖 `/proc` 权威路径或 `psutil` 跨平台路径，并验证 identity 的 match/mismatch 语义。
- 运行本目录测试时使用进程外超时和内存上限，产物保留在对应 `out/tests/unit/services/infrastructure/node_debug/` 路径。
