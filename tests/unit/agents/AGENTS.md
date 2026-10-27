# 目录用途

镜像 `app/agents/`，存放 Agent 组装、Provider、中间件、策略和内置工具的单元测试。

## 按职责查找

下图是职责索引；`policy/`、`providers/`、`tooling/` 和 `tools/` 已按职责分组，其余叶节点测试目前仍在本目录根部。

```text
tests/unit/agents/
├── conftest.py                  共享 Agent 测试依赖
├── policy/                      工具策略解析与分组
├── providers/                   Provider 请求、流式生命周期与上游追踪
├── tooling/                     自定义工具配置与模型可见 schema
├── tools/                       内置工具执行与固定扩展工具入口
└── 根目录测试（按职责检索）
    ├── Agent 组装与模型调用
    │   ├── test_agent_factory*.py
    │   ├── test_graph_binding*.py
    │   ├── test_model_call*.py
    │   └── test_model_capability_routing.py
    ├── Provider 配置（根目录）
    │   └── test_provider_api_mode_config.py
    ├── 上下文、checkpoint 与摘要
    │   ├── test_checkpoint_recovery.py
    │   ├── test_context_source_boundary_audit.py
    │   └── test_*summarization*.py
    ├── 工具装配与调用中间件
    │   ├── test_tool_confirmation_middleware.py
    │   ├── test_structured_tool_call_middleware.py
    │   ├── test_tool_*
    │   └── test_llm_logging_middleware.py
    ├── 工作区文件与资源导航
    │   ├── test_codex_*_tool*.py
    │   ├── test_file_navigation_deadline.py
    │   ├── test_resource_*tool*.py
    │   └── test_workspace_backend.py
    └── 指令与 Skill 生命周期
        ├── test_instruction_producers.py、test_middleware_prompts.py
        └── test_skill_*.py
```

根目录目前有多个直接测试模块，新增独立职责时优先放入有明确边界的子目录并补齐本地 `AGENTS.md`；成组迁移现有测试时同步更新测试矩阵标识和镜像输出路径，避免只移动文件却留下旧归属。

## 可修改内容

- Agent 组件的隔离测试与测试专用 fake。
- `policy/`、`providers/`、`tools/` 等生产子模块对应的测试分区。
- `tooling/` 自定义工具配置与模型可见 schema 合同。

## 不可修改内容

- 不调用真实模型 Provider、真实网络或完整 Agent 运行链路。
- 不在此放 Gateway、API 或服务层测试。

## 规范

- 测试文件按主要被测的 `app/agents/` 模块命名和放置。
- 外部 Provider、工具和运行时依赖通过 pytest fixture 显式替换，失败不得静默忽略。
