# 目录用途

`tests/fixtures/workspaces/` 存放可复制生成正式测试工作区的完整静态 fixture。

## 工作区模板树

```text
workspaces/
├── default_test_workspace/                 通用项目文件和多模态输入
│   ├── .boxteam/workspace-identity.json    固定工作区身份
│   ├── assets/                             图片、视频等输入资源
│   └── src/                                示例工程源文件
├── browser_state_store_workspace/          浏览器持久化 owner 的轻量完整工作区
│   └── .boxteam/workspace-identity.json    固定工作区身份
├── terminal_cwd_workspace/                 终端 cwd 生命周期测试所需的最小工作区
│   └── .boxteam/workspace-identity.json    固定工作区身份
├── model_tool_test_workspace/              apply_patch、edit_file 场景输入
│   ├── apply_patch/<scenario>/              补丁工具的最小初始文件
│   └── edit_file/exact_replacement/          精确替换的目标文本样本
└── custom_tool_test_workspace/              扩展工具说明与历史生成清单
    ├── rollout-fixture.json                 运行时历史生成器的输入
    └── .boxteam/
        ├── AGENTS.md                        测试工作区约束
        └── skills/                          模板内的测试专用 Skill
```

扩展工具历史场景通过 `scripts/generate_custom_tool_rollout_fixture.py` 在隔离工作区生成 `fixture/<turn>.json` 与 rollout 数据；这些生成物不存放在模板源目录。根据测试要验证的边界选择最小模板：仅验证 BrowserStateStore/BrowserManager 身份与持久化、且不读取项目文件时用 `browser_state_store_workspace/`；只验证终端 cwd 解析、创建、落盘与恢复时用 `terminal_cwd_workspace/`，需要具体目录的用例在复制后创建；通用项目行为用 `default_test_workspace/`，模型对文件工具的输入输出用 `model_tool_test_workspace/`，扩展工具说明或历史生成场景用 `custom_tool_test_workspace/`。运行时只改复制到对应 `out/tests/` 的工作区。

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
