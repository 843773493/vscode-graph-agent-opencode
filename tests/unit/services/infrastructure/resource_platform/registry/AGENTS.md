# 目录用途

镜像 `app/services/infrastructure/resource_platform/registry/`，测试语义快照登记、identity、成员值和原子发布合同。

```text
registry/
├── AGENTS.md
└── test_resource_registry.py  # descriptor、identity、members、CAS 与并发 capture
```

# 可修改内容

- `ResourceRegistry` 的 descriptor 登记、CAS、last-valid 保留、捕获一致性与纯值对象合同测试。
- 通过父目录 fixture 注入的隔离 registry，以及本目录 fixture 提供的不可变 snapshot 和成员样本。

# 不可修改内容

- 不测试来源读取、loader 或派生图拓扑；这些行为归 `../derivation/`。
- 不访问真实文件系统、网络或外部服务。

# 规范

- 每个测试独立验证一个可观察合同，fixture 只共享稳定的测试数据。
- 并发合同通过同步事件和明确等待验证，不依赖任意长 sleep。
