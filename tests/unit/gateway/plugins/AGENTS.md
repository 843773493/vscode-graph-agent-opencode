# 目录用途

验证 Gateway 自有插件包解析与发布合同。

# 可修改内容

- `app/gateway/plugins/` 包解析、路径校验与安装行为的单元测试。

# 不可修改内容

- 不启动 Gateway、Workspace 或插件进程。
- 不验证或复刻 runtime 生命周期状态机。

# 规范

- 使用 pytest 临时目录，不访问用户 Home 或共享开发 Home。
- 测试包入口和 Skill 必须由 `plugin.jsonc` 显式声明，不能用目录名作为发现依据。
- 正式产物使用 `out/tests/unit/gateway/plugins/`；Agent 临时产物只用 `out/tests/temp/YYYY/MM/DD/plugin_packages_d21/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
