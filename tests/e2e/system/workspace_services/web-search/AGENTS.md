# 目录用途

存放 Web 搜索、网页抓取以及相关扩展工具的端到端测试。

## 文件职责树

```text
web-search/
└── test_web_search_custom_tools.py
    ├── 工作区 fixture：测试专用 Skill 模板 + resources/skills/web-search-fetch
    └── 验收：真实 DuckDuckGo 调用、网页抓取结果与工具 trace
```

fixture 只复制场景实际读取的产品 Skill；新增单一 Skill 场景时也按需复制，避免把整个 `resources/skills/` 集合带入每个工作区。

# 可修改内容

- Web 搜索和网页正文抓取的真实模型 E2E。
- 本目录测试使用的最小 fixture 与断言辅助函数。

# 不可修改内容

- 不放浏览器交互、gateway 或 terminal 工具测试。
- 不使用伪造的模型响应代替真实工具调用。

# 规范

- 每个测试文件独立准备工作区、端口和后端进程。
- 搜索 E2E 使用真实 DuckDuckGo 请求，并断言真实工具 trace。
