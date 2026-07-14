# 目录用途

`asset/custom_tool_test_workspace/` 是扩展工具 e2e 测试使用的工作区模板。

# 可修改内容

- 可以调整测试所需的最小工作区文件。
- 可以维护用于告诉模型如何调用扩展工具的普通工作区说明。

# 不可修改内容

- 不要提交测试运行时生成的 `.boxteam/checkpoints`、`.boxteam/logs`、`.boxteam/sessions` 或缓存产物。
- 不要把真实用户工作区数据放入该模板。

# 规范

- 模板应保持最小化，只保留测试输入文件。

## 扩展工具

当前工作区配置了扩展工具 skill。扩展工具不会直接出现在模型的 tools 列表中，模型应先根据用户提到的扩展工具名称找到对应 skill，再读取该 skill 的完整说明。

- 当用户要求调用 `test_tool_2` 时，读取 `/.boxteam/skills/test-tool-2/SKILL.md`。
- 当用户要求调用 `large_test_output` 或验证大工具输出落盘时，读取 `/.boxteam/skills/large-test-output/SKILL.md`。
- 当用户要求读取另一个会话最近 N 轮消息，或 grep/read 该会话当前模型上下文 JSONL 时，读取 `/.boxteam/skills/read-session-recent-text-messages/SKILL.md`。
- 当用户要求搜索互联网、搜索新闻或抓取网页正文时，读取 `/.boxteam/skills/web-search-fetch/SKILL.md`。
- 当用户要求打开或操控浏览器页面、调用 browser 控制工具、执行 clickElement/dragElement/handleDialog/hoverElement/navigatePage/openBrowserPage/readPage/runPlaywrightCode/screenshotPage/typeInPage 时，读取 `/.boxteam/skills/browser-control/SKILL.md`。
- 不要根据本文件猜测调用参数；具体固定入口名称、目标工具名和参数必须以对应 skill 为准。

读取 skill 后，必须发起真实工具调用，不要只描述调用计划。
