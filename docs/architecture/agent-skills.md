# Agent Skill 资源布局

## 当前源码布局

产品级扩展工具说明统一维护在项目根目录 `resources/skills/`。每个目录代表一个可配置的 Skill 组，至少包含 `SKILL.md` 和该目录的 `AGENTS.md`：

```text
resources/
├── AGENTS.md
└── skills/
    ├── AGENTS.md
    ├── browser-control/
    │   ├── AGENTS.md
    │   └── SKILL.md
    ├── gateway-context/
    │   ├── AGENTS.md
    │   └── SKILL.md
    └── web-search-fetch/
        ├── AGENTS.md
        └── SKILL.md
```

`asset/` 是只读测试模板，不是产品 Skill 源码目录。扩展工具 E2E 测试创建隔离工作区时，会把 `resources/skills/` 下的共享 Skill 复制到目标工作区的 `/.boxteam/skills/`；模板自身只保留 `test-tool-2`、`large-test-output` 等测试专用 Skill。

## 运行时加载链路

Gateway 配置中的 `runtime.workspace.default_skill_groups` 是默认启用列表，默认值位于 `configs/gateway_inline.jsonc`。Gateway 启动或重启受管 Workspace 时，将列表序列化到 `BOXTEAM_DEFAULT_SKILL_GROUPS` 环境变量；Workspace Agent 运行时解析该变量，并把对应发行包资源挂载为只读的 `/.boxteam/bundled-skills/`。

Skill source 的优先级从低到高为：

1. `/.boxteam/bundled-skills/`：发行包共享资源，只读，由 Gateway 配置选择组。
2. `/.boxteam/skills`：当前工作区资源，可覆盖同名共享 Skill，并承载工作区专用 Skill。

这两个虚拟路径都由 `app/agents/workspace_backend.py` 的 `CompositeBackend` 提供。内置资源只允许读取，不能通过 Agent 的 `write`、`edit` 或文件上传接口修改。工作区 Skill 仍属于工作区业务数据边界；Gateway 不读取工作区 `.boxteam/`。

Skill 的“可发现、可读取”与扩展工具的“实际启用”是两件事：Skill 负责告诉模型何时以及如何使用扩展工具，工具是否出现在当前 Agent 的工具策略中仍由 Workspace 配置、工具工厂和策略代码决定。新增扩展工具时必须同时检查这两条链路。

## 动态扩展工具契约

`invoke_custom_tool` 的固定外层 schema 已经作为模型工具内置提供：模型调用它时传入 `tool_name` 和 `arguments`。Skill 不重复复制这份固定 schema，而是在工具组正文中为每个目标工具提供以下 JSON 对象：

```json
{
  "tool_name": "web_search",
  "arguments_schema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query": {"type": "string", "minLength": 1},
      "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10}
    }
  }
}
```

`arguments_schema` 是目标工具 `arguments` 对象的 JSON Schema。由于固定入口的完整调用结构已经由模型工具 schema 提供，Skill 不再重复写完整调用示例。JSON Schema 负责字段、类型、默认值、枚举和数值边界；工具调用顺序、前置条件、跨字段规则和安全边界用短自然语言补充。表格可以作为人工维护或展示工具，但不是动态契约的规范格式。新增内置、插件或 MCP 扩展工具时，应从统一的模型可见工具 schema 或 Tool Registry 生成/校验这段文档，避免 Skill 手工定义与实际运行时漂移。

## 新增 Skill 组

按以下顺序修改，保持源码、默认配置和发行包一致：

1. 在 `resources/skills/<skill-group>/` 新增 `AGENTS.md` 与 `SKILL.md`，Skill 组 ID 使用小写字母、数字和连字符，并以小写字母开头。
2. 如果该组应随项目默认启用，把 ID 加入 `configs/gateway_inline.jsonc` 的 `runtime.workspace.default_skill_groups`；同步检查 `configs/gateway_schema.jsonc` 和 `configs/gateway_dev.jsonc`。
3. 如果 Skill 使用新的扩展工具，补齐 Workspace 工具工厂、工具策略和 `configs/workspace_inline.jsonc` 中的默认工具配置；不要把工具启用逻辑放进 Gateway。
4. 在相关 E2E 或单元测试中验证 Skill metadata、完整文件读取和真实工具调用；测试工作区使用 `tests/support/workspaces.py` 从 `resources/skills/` 复制共享 Skill，不要直接修改 `asset/` 模板中的产品副本。
5. 确认 `packaging/runtime/build-linux-x64.mjs` 与 `build-windows-x64.mjs` 将 `resources/` 复制到 `application/resources/`，并保留 runtime manifest 的 `skill_resources` 路径。

不需要为每个 Skill 组新增 Python 路由。运行时会校验配置的组 ID、资源目录和 `SKILL.md`；未知或缺失资源应直接报错，不能静默跳过。
