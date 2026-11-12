# Agent Skill 资源布局

## 当前源码布局

产品 Skill 按来源 owner 存放。随代码插件发布的 Browser、Debug 说明位于插件包 `resources/plugins/developer-tools/skills/`，并在插件 manifest 中声明；独立资源 `gateway-context` 和 `web-search-fetch` 位于项目根目录 `resources/skills/`：

```text
resources/
├── skills/
│   ├── gateway-context/SKILL.md
│   └── web-search-fetch/SKILL.md
└── plugins/developer-tools/
    ├── plugin.jsonc
    └── skills/
        ├── browser-control/SKILL.md
        └── debugging/SKILL.md
```

`asset/` 是只读测试模板，不是产品 Skill 源码目录。插件 Skill 不复制到工作区；Gateway 插件来源按原资源 URI 提供已安装包中的内容。测试应通过相应 owner 安装／启用插件并读取来源。工作区 `.boxteam/skills/` 只用于工作区自身的 Skill。

## 运行时加载链路

Gateway 配置中的 `runtime.workspace.default_skill_groups` 仅选择根目录 `resources/skills/` 中的内置资源组，默认值位于 `configs/gateway_inline.jsonc`。Gateway 启动或重启受管 Workspace 时，将列表序列化到 `BOXTEAM_DEFAULT_SKILL_GROUPS`；Workspace Agent 解析该列表。插件 Skill 由插件配置 owner 发布到现有 Gateway Skill 来源，不经过 bundled group，也不复制到 Workspace。

Skill source 的优先级从低到高为：

1. `/.boxteam/skills`：当前工作区资源。
2. Gateway、network 与 inline 来源由现有 Skill catalog 读取和解析；插件 URI 由原 Gateway 插件资源 owner 提供。

工作区文件后端提供工作区 Skill 目录。Gateway／插件资源按来源 owner 提供只读内容；Agent 不能通过 `write`、`edit` 或文件上传接口修改安装包。Gateway 不读取工作区 `.boxteam/`。

Skill 的“可发现、可读取”与扩展工具的“实际启用”是两件事：Skill 负责告诉模型何时以及如何使用扩展工具，工具是否出现在当前 Agent 的工具策略中仍由 Workspace 配置、工具工厂和策略代码决定。新增扩展工具时必须同时检查这两条链路。

## 动态扩展工具契约

`invoke_extension_tool` 的固定外层 schema 已经作为模型工具内置提供：模型调用它时传入 `tool_name` 和 `arguments`。Skill 不重复复制这份固定 schema，而是在工具组正文中为每个目标工具提供以下 JSON 对象：

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

1. 独立内置 Skill 组在 `resources/skills/<skill-group>/` 新增 `SKILL.md`；随插件发布的说明放入插件已声明目录，名称和描述从 frontmatter 派生。
2. 如果该组应随项目默认启用，把 ID 加入 `configs/gateway_inline.jsonc` 的 `runtime.workspace.default_skill_groups`；同步检查 `configs/gateway_schema.jsonc` 和 `configs/gateway_dev.jsonc`。
3. 如果 Skill 使用新的扩展工具，补齐 Workspace 工具工厂、工具策略和 `configs/workspace_inline.jsonc` 中的默认工具配置；不要把工具启用逻辑放进 Gateway。
4. 在相关测试中验证 metadata、完整 URI 读取和真实工具调用；不要将产品 Skill 复制到工作区 fixture。
5. 插件 Skill 随插件 ZIP 发布；只有明确选择的独立资源组才从 `resources/skills/` 读取。

不需要为每个 Skill 新增业务 API。各来源 owner 校验其声明及资源；未知或缺失资源应明确报错，不能静默跳过。
