# 目录用途

`asset/custom_tool_test_workspace/` 是扩展工具 e2e 测试使用的工作区模板。

# 可修改内容

- 可以调整测试所需的最小工作区文件。
- 可以维护用于告诉模型如何调用扩展工具的普通工作区说明。

# 不可修改内容

- 不要提交测试运行时生成的 `.boxteam/checkpoints`、`.boxteam/logs` 或缓存产物。
- `.boxteam/sessions` 仅可提交明确标注的静态 rollout fixture，不能写入运行时变更；其中可以包含预生成、明确标注用途的真实模型响应快照。
- 不要把真实用户工作区数据放入该模板；真实模型快照必须是专门生成的测试资源，不得包含用户隐私或运行时状态。

# 规范

- 模板应保持最小化，只保留测试输入文件。
- mock rollout fixture 用于验证历史加载，不代表真实用户数据；其会话内容必须是确定性的测试数据。
- 真实模型快照只用于验证多 provider、reasoning、summary、encrypted reasoning 和 checkpoint 投影，测试运行时只读，不得重新生成或覆盖。

## 扩展工具

产品级扩展工具 skill 的唯一源码位于项目根目录 `resources/skills/`。E2E 测试在创建隔离工作区时把这些共享 skill 复制到目标工作区的 `.boxteam/skills/`，因此本模板不再重复维护产品 skill 文件。模板内只保留测试专用 skill；扩展工具不会直接出现在模型的 tools 列表中，模型应先根据用户提到的扩展工具名称找到对应 skill，再读取该 skill 的完整说明。

- 当用户要求调用 `test_tool_2` 时，读取 `.boxteam/skills/test-tool-2/SKILL.md`。
- 当用户要求调用 `large_test_output` 或验证大工具输出落盘时，读取 `.boxteam/skills/large-test-output/SKILL.md`。
- 当用户要求查看、搜索或分段读取其他 Session、工作区或 Gateway 上下文时，读取共享 skill 复制后的 `.boxteam/skills/gateway-context/SKILL.md`。
- 当用户要求搜索互联网、搜索新闻或抓取网页正文时，读取共享 skill 复制后的 `.boxteam/skills/web-search-fetch/SKILL.md`。
- 当用户要求列出、打开或操控浏览器页面、查询当前后台浏览器网站、调用 browser 控制工具、执行 clickElement/dragElement/handleDialog/hoverElement/listBrowserPage/navigatePage/openBrowserPage/readPage/runPlaywrightCode/screenshotPage/typeInPage 时，读取共享 skill 复制后的 `.boxteam/skills/browser-control/SKILL.md`。
- 不要根据本文件猜测调用参数；具体固定入口名称、目标工具名和参数必须以对应 skill 为准。

读取 skill 后，必须发起真实工具调用，不要只描述调用计划。
