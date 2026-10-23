# 目录用途

镜像 `app/services/infrastructure/resource_platform/derivation/`，测试有向无环派生图如何注册规范、读取已接受的来源 revision 并发布语义快照。

```text
derivation/
├── AGENTS.md
└── test_resource_derivation_graph.py  # 拓扑、来源代际、派生与批量发布
```

# 可修改内容

- 派生图拓扑、依赖代际、来源不可用、loader 行为和批量发布的单元测试。
- 通过父目录 fixture 注入新建 registry，并为每项测试隔离来源和 graph。

# 不可修改内容

- 不覆盖语义 registry 自身的 identity、成员值和 CAS 合同；这些测试归 `../registry/`。
- 不读取真实工作区或启动后端服务。

# 规范

- 文件和测试名描述受测行为，使用 `tmp_path` 隔离来源文件。
- 批量行为测试要断言依赖顺序与发布结果，并避免通过实现细节之外的时间阈值判断性能。
