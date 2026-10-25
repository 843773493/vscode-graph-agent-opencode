# 目录用途

集中存放会话导航 mutation 的两种测试入口：通过真实 HTTP API 提交并等待 operation，以及直接构造并提交 catalog intent。

```text
session_navigation/
├── http_operations.py  HTTP intent 提交、回执核对和终态等待
└── catalog_intents.py  catalog revision 绑定、intent 构造和 owner 提交
```

# 可修改内容

- 经 HTTP API 验证 durable acceptance 与 operation 终态的测试辅助。
- 直接调用 catalog 服务时构造 revision-bound intent 的测试辅助。

# 不可修改内容

- 不放产品导航业务规则或测试用例。
- 不用 direct catalog helper 替代需要证明 HTTP 路由、序列化或 Gateway 行为的场景。
- 不把 operation 回执或物理 settlement 合成为虚假成功。

# 规范

- 按要验证的边界选择入口：HTTP helper 证明 API 链路，catalog helper 只证明 owner 合同。
- HTTP 等待使用单一总期限；超时只结束观察，不取消已受理的后台 operation。
- 新辅助按入口边界命名，并同步更新 `tests/support/AGENTS.md` 职责树。
