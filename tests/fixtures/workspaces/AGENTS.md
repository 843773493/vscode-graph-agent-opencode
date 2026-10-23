# 目录用途

`tests/fixtures/workspaces/` 存放可复制生成正式测试工作区的完整静态 fixture。

## 工作区模板树

```text
workspaces/
├── default_test_workspace/                 通用项目文件和多模态输入
│   ├── .boxteam/workspace-identity.json    固定工作区身份
│   ├── assets/                             图片、视频等输入资源
│   └── src/                                示例工程源文件
├── model_tool_test_workspace/              apply_patch、edit_file 场景输入
│   ├── apply_patch/<scenario>/              补丁工具的最小初始文件
│   └── edit_file/exact_replacement/          精确替换的目标文本样本
└── custom_tool_test_workspace/              扩展工具与历史加载输入
    ├── fixture/                             扩展工具读取的编号 JSON 文件
    ├── rollout-fixture.json                 静态会话的生成清单
    └── .boxteam/
        ├── skills/                          测试专用 Skill
        ├── sessions/                        静态 rollout 会话
        └── navigation/                      会话目录索引
```

根据测试要验证的边界选择最小模板：通用工作区行为用 `default_test_workspace/`，模型对文件工具的输入输出用 `model_tool_test_workspace/`，扩展工具说明或静态历史加载用 `custom_tool_test_workspace/`。运行时只改复制到对应 `out/tests/` 的工作区。

# 可修改内容

- 可以维护测试所需的工作区输入文件、静态会话、rollout JSONL、SQLite 索引和导航索引。
- 可以新增经过测试验证的确定性边界场景 fixture。

# 不可修改内容

- 不放测试运行时生成的日志、checkpoint、截图、缓存或真实用户数据。
- 不在这里实现产品源码；运行时工作区必须复制到 `out/tests/` 后再修改。

# 规范

- 测试通过 `tests/support/workspaces.py` 从本目录复制工作区。
- 静态会话必须同时维护 `session.json`、`rollout/rollout.jsonl`、`rollout/index.sqlite` 和权威 `navigation/session-catalog.sqlite`。
- fixture 内容必须确定性、可复查，并与对应测试场景保持明确命名。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
