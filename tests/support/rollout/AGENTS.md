# 目录用途

集中存放 canonical rollout fixture 的输出提交、view 成员损坏注入和 Web 历史边界播种。

```text
rollout/
├── fixture_output.py      经真实执行 owner 提交静态历史输出
├── view_mutations.py      隔离修改 view 成员并重建私有分块索引
└── web_boundary_seeding.py 生成 Web 边界场景的 canonical 历史
```

# 可修改内容

- 由测试调用的 canonical 历史 fixture 准备。
- 只作用于测试连接的 view 成员损坏注入。
- 显式测试工作区中的 Web 边界历史播种。

# 不可修改内容

- 不放 rollout 产品实现或测试用例。
- 不绕过 admission、checkpoint、canonical item 或执行终态 owner 伪造成功历史。
- 不写 fixture 源目录或测试运行产物。

# 规范

- 静态输出必须经过正式执行 owner；view 损坏必须隔离在测试连接内。
- 场景数据使用调用方提供的隔离工作区和稳定身份，不从当前日期或显示名推导路径。
- 新增文件按历史准备阶段命名，并同步更新 `tests/support/AGENTS.md` 的职责树。
