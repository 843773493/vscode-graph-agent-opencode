# 目录用途

`tests/fixtures/workspaces/custom_tool_test_workspace/` 是扩展工具 e2e 和历史加载测试使用的完整工作区 fixture。

`rollout-fixture.json` 是静态生成清单。编号文件 `fixture/<turn>.json` 由 `scripts/generate_custom_tool_rollout_fixture.py` 在隔离工作区中生成，属于运行时输出，不保存在模板源目录。

# 可修改内容

- 可以调整测试所需的工作区输入文件、静态会话和导航索引。
- 可以维护用于告诉模型如何调用扩展工具的普通工作区说明。

# 不可修改内容

- 不要提交测试运行时生成的 `.boxteam/checkpoints`、`.boxteam/logs` 或缓存产物。
- `.boxteam/sessions` 仅可提交明确标注的静态 rollout fixture，不能写入运行时变更；其中可以包含预生成、明确标注用途的真实模型响应快照。
- 不要把真实用户工作区数据放入该模板；真实模型快照必须是专门生成的测试资源，不得包含用户隐私或运行时状态。

# 规范

- fixture 应保持最小化，只保留测试输入和确定性的静态历史数据。
- mock rollout fixture 用于验证历史加载，不代表真实用户数据；其会话内容必须是确定性的测试数据。
- 真实模型快照只用于验证多 provider、reasoning、summary、encrypted reasoning 和 checkpoint 投影，测试运行时只读，不得重新生成或覆盖。

## 扩展工具

产品级扩展工具 Skill 由各来源 owner 提供：Browser／Debug 说明随 `developer-tools` 插件发布，`gateway-context` 等独立资源按其既有来源发布。不要把产品 Skill 复制进测试工作区；本模板只保留测试专用 Skill。扩展工具不会直接出现在模型的 tools 列表中，模型应先根据用户意图调用 `skill_load(name=...)` 加载对应说明；Skill 正文由系统注入，不应使用 `read_file` 绕过来源 owner。

- 当用户要求调用 `test_tool_2` 时，调用 `skill_load(name="test-tool-2")`。
- 当用户要求调用 `large_test_output` 或验证大工具输出落盘时，调用 `skill_load(name="large-test-output")`。
- 当用户要求查看、搜索或分段读取其他 Session、工作区或 Gateway 上下文时，调用 `skill_load(name="gateway-context")`。
- 当用户要求搜索互联网、搜索新闻或抓取网页正文时，调用 `skill_load(name="web-search-fetch")`。
- 当用户要求列出、打开或操控浏览器页面、查询当前后台浏览器网站、调用 browser 控制工具、执行 clickElement/dragElement/handleDialog/hoverElement/listBrowserPage/navigatePage/openBrowserPage/readPage/runPlaywrightCode/screenshotPage/typeInPage 时，调用 `skill_load(name="browser-control")`。
- 不要根据本文件猜测调用参数；具体固定入口名称、目标工具名和参数必须以对应 skill 为准。

读取 skill 后，必须发起真实工具调用，不要只描述调用计划。
