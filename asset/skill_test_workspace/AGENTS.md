# 目录用途

`asset/skill_test_workspace/` 是 skill 加载与隐藏工具 e2e 测试使用的工作区模板。

# 可修改内容

- 可以调整测试所需的最小工作区文件。
- 可以维护 `.boxteam/skills/` 下的测试 skill。

# 不可修改内容

- 不要提交测试运行时生成的 `.boxteam/checkpoints`、`.boxteam/logs`、`.boxteam/sessions` 或缓存产物。
- 不要把真实用户工作区数据放入该模板。

# 规范

- 模板应保持最小化，只保留测试输入文件。
- skill 文档应遵循 Agent Skills frontmatter 规范。
