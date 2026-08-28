## Context

历史加载的权威数据已经统一到会话 rollout：`rollout/rollout.jsonl` 只追加已提交的 canonical message，`index.sqlite` 保存消息、Turn、checkpoint、branch、view 和 offset 索引。Trace、日志以及旧的 `turn_history` 投影只服务于诊断或运行时事件，不能参与聊天主时间线重建。

本变更只定义历史读取和 Web 默认体验；rollout 的增量持久化、SQLite context view、checkpoint saver、fork、rewind、pruning 和崩溃恢复由 `refactor-rollout-checkpoint-storage` 负责。这样不会重新引入物理 chunk，也不会让展示层复制一份消息历史。

## Decisions

### 1. 使用 rollout-backed reader

`RolloutHistoryReader` 通过 SQLite 的 Turn/record offset 定位 JSONL 区间，再 materialize 所请求的有限消息。支持以下方向：

```text
head     从历史起点向后
tail     从 active head 向前
before   游标之前
after    游标之后
around   游标两侧
```

默认单位是完整 Turn。用户消息是普通对话的分隔点；合并 steering、工具调用/结果、内部消息和模型终态属于同一 Turn，不得被分页拆开。没有有效 rollout 时必须返回可诊断错误，不能从 `logs/traces/*` 或旧 projection 静默补数据。

### 2. 通过 include 选择展示内容

LoadPlan 使用结构化参数：方向、opaque cursor、Turn 范围、include 和服务端预算。include 至少支持 `user`、`assistant_text`、`thinking`、`final_response`、`tool_summary`、`tool_call`、`tool_result`、`internal` 和 metadata。默认工具项只返回工具名与状态；工具参数和结果只有在显式请求且未超过硬上限时才返回。

历史和详情继续使用同一 `/history` 语义：普通分页使用方向/cursor，当前 Turn 或可视 Turn 详情使用最多 4 个 `turn_ids`。详情响应携带 projection epoch、Turn revision、有限结果和继续游标；不能为了生成摘要读取完整大型 payload。

### 3. 稳定游标和边界

游标绑定 rollout、active branch、projection epoch 和历史锚点。普通追加、`continue/resume`、Turn revision 更新和 checkpoint 提交不使游标失效；`rewind`、replay 中的 rewind、删除、branch 切换或无法保持身份的重建推进 epoch，并返回明确 stale-cursor 错误。

`rewind` 只移动 active head 并创建逻辑 branch；`continue/resume` 从当前 head 追加；`replay` 是 `rewind + 可选编辑/替换 + continue`。新的 SQLite context view 由 compaction、rewind、fork seed 等上下文边界创建，不能因为 replay 这个业务名称单独创建，也不按字节切物理 chunk。

### 4. Gateway 默认渐进策略

历史策略由会话所属 Gateway 解析，使用可扩展的嵌套内联配置。无已保存视图锚点时仍从尾部加载最新 1 个 Turn；有已保存锚点时统一使用 `around(anchor)`，不再按滚动次数隐式切换 4/16/64 批次：

```jsonc
{
  "features": {
    "session_history": {
      "loading": {
        "progressive": {
          "initial": {
            "turns": 1,
            "include": ["user", "thinking", "tool_summary", "final_response"]
          },
          "anchor": {
            "before_turns": 4,
            "after_turns": 4,
            "include": ["user", "final_response"]
          }
        }
      }
    }
  }
}
```

首次进入只加载最新 1 个 Turn。若 Gateway 保存了该会话的视图锚点，前端请求 `around(anchor)`，一次返回锚点前后各 4 个 Turn，默认投影为 `user + final_response`，并返回 `before_cursor`、`after_cursor`。继续向上只提交 `before_cursor`，继续向下只提交 `after_cursor`；两侧窗口使用所属 Gateway 的 `anchor` 配置。通过 Gateway A 访问 Gateway B 的会话时，B 的配置和响应优先，A 不覆盖 B 的策略。

### 5. Web Turn 活动统计与详情

每个历史 Turn 始终渲染一条活动统计/折叠行，显示耗时、隐藏消息数以及中间 assistant、tool_call、tool_result 数量。折叠箭头收起时指向右侧，展开时指向下方。点击该行通过 `/history` 的当前 Turn 详情接口加载并展开 thinking、工具摘要、tool_call 和 tool_result；不得直接读取 JSONL。当前 Turn 的 assistant avatar/codicon 继续作为可点击工具详情入口，该选项只影响当前 Turn。其它 Turn、会话和 Gateway 默认加载策略不变。

### 6. 有界读取与失败透明

append-only 普通历史优先走 SQLite offset 快速路径，只读取命中的 Turn 区间；涉及 context view 边界或未索引记录时走明确的恢复路径。所有路径都执行服务端硬上限。文件先写并提交后再更新 SQLite，已提交历史只能按 SQLite 权威状态读取；损坏或 cursor 失效必须返回原因，不伪造空历史或静默回退旧文件。

## Test Strategy

- unit：验证 rollout JSONL、SQLite offset、LoadPlan、cursor、include 投影、semantic boundary、checkpoint saver 和提交/恢复边界。
- integration：使用确定性 stub provider 与隔离 workspace，直接通过 rollout/SQLite 验证从头、从尾、游标前 1 个、游标上下各 1 个、游标前 5 个 Turn 的加载；验证用户消息 + 工具摘要 + 最终响应，以及用户消息 + tool_call + tool_result + 最终响应。
- Web：用 stub API 验证首次 1 Turn、`around(anchor)`、before/after 双向游标、滚动锚点、活动统计折叠行和当前 Turn 详情立即重载；使用资产中的大型工具 mock 会话，不依赖真实模型。
- Fixture：`test_real_rollout_fixture.py` 从复制后的资产工作区读取真实 128 Turn 会话，验证多 provider 与 SQLite reader；浏览器测试读取复制后的大型工具 mock 会话，验证固定 UI 投影和大型正文有界详情。任何测试都不得直接写入资产目录或在运行时重新生成会话。
- 不新增真实模型 E2E，不新增 20+ 会话摘要基准；正式工作区和产物遵循 `tests/` 到 `out/tests/` 的镜像规则。

### 7. 投影读取与消息语义

历史 API 不拆 canonical LangChain `AIMessage`。写入 checkpoint 时由存储层同步生成轻量 message/Turn/tool/thinking projection index；`thinking` projection 保留 `reasoning`、`summary`、`encrypted` 来源，历史 thinking blocks 只读取这些索引，完整 `assistant_text`、tool_call 参数和 tool_result 正文通过命中的 JSONL offset 做轻量 JSON 解析，不构造完整 LangGraph checkpoint state。

`final_response` 由 core change 写入的 `turn_finalize.final_message_sequence` 定位；`assistant_text` 只包含其它中间可见 assistant text block。`thinking` 返回可读 `reasoning` block、安全 `summary` block 或无正文的 `encrypted` 标记，provider encrypted reasoning 原文只保存和回放，不向浏览器返回。

无已保存锚点的最新 1 个 Turn 默认请求：

```text
user + thinking(若存在) + tool_summary + final_response
```

锚点窗口和其后的 before/after 页面默认请求 `user + final_response`。活动统计来自 SQLite projection；reasoning、tool summary 和工具原文均在统计行内按需展开，当前 Turn 详情才显式请求 tool_call/tool_result。

### 8. 高性能读取路径

一次 history 请求使用一个 read snapshot 和一次页面级 SQLite query，按 `rollout.jsonl` offset 批量读取。单个 rollout 文件采用复用句柄的顺序读取；默认路径不得执行全文件 integrity scan、LangGraph checkpoint materialize 或逐 Turn initialize。

性能验收使用资产中的 128 Turn 混合 fixture，通过 8011 全链路测量 bootstrap、`around(anchor)`、before/after 双向加载和当前 Turn 详情；服务端 p95 优化目标为 100ms、硬验收上限为 200ms，热路径 prepend/append 与锚点恢复硬验收上限为 200ms。Composer 可交互性和统计行折叠行为必须同时验证。
