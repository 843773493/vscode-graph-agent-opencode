## Context

See `proposal.md` and the capability deltas for the externally visible contract. 当前实现已经有会话附件目录、`AttachmentRef`、缩略图接口、`HumanMessage` checkpoint 和 LiteLLM 的 assistant content 投影，但几个边界尚未统一：

- `RolloutHistoryReader` 对列表形式的用户 content 仍可能整体 `json.dumps`，从而把 `image_url` data URL 当作用户正文。
- `AttachmentContentService` 已经会生成附件 manifest 和图片 block，但原件、预览、模型请求 content 与 Web 展示 content 没有清晰的三种投影边界。
- `AIMessage` 已经按有序 block 保存并按目标 provider 临时投影；用户消息尚未复用相同的不可变 source 思路。
- 当前附件缩略图默认边界与本变更的 512 像素规则不一致，且附件存储入口对 MIME 类型的处理过早限制在图片、音频、视频。

## Goals / Non-Goals

**Goals:**

- 建立一个可遍历的 canonical 用户 content 序列，并让 checkpoint、rollout、provider 请求和 Web 历史都从这个 source 生成各自投影。
- 保留模型使用的图片预览 block（包括 preview data URL/base64）以支持精确 checkpoint/history roundtrip，同时禁止它进入默认用户正文展示。
- 让附件原件、预览变体、稳定身份、相对路径和 MIME 元数据可被后端、provider adapter 和 Web 共同使用。
- 用 LiteLLM 处理共通 provider content，在 Chat Completions、Responses、Anthropic Messages 等边界保留必要的 provider-specific mapper。
- 在 rich block 不可发送、预览生成失败或资源读取失败时留下明确诊断，并保留模型可组合现有工具使用的附件路径。

**Non-Goals:**

- 不新增 `attachment.xxx` 工具、文件解析工具或模型工具协议；附件访问继续依赖现有文件、终端和查看能力。
- 不新增 PDF/document 之类的应用级 `image/video/audio` modality。PDF、文档和其它 MIME 作为通用附件保存，是否能直接映射为目标 provider block 由 adapter 决定。
- 不把完整原始附件、原始图片 base64 或未界定的大型文档正文塞入用户 message；message 只携带相对路径、元信息和受界限的 preview block。
- 不要求 Web 直接理解所有 provider block，也不让 Web 通过反解析展示文本恢复附件身份。

## Decisions

### 1. 三层投影：canonical source、provider request、display projection

采用以下单向数据流：

```text
用户输入/附件
    │
    ├─ 原件 + preview variants ──> 会话附件存储
    │
    └─ canonical HumanMessage.content（有序 blocks）
          ├─ checkpoint / rollout：保存 source，不做展示清洗
          ├─ provider projection：按目标 provider 和能力生成临时 request
          └─ history display projection：只提取 user text + attachment DTO
                                      └─ Web preview / 右侧会话资源
```

`HumanMessage.content` 是不可变 source。它可以包含文本 block、由 `UserContentBuilder` 生成的 `<attachment ...>` manifest，以及可选的 `image_url`/等价 rich block。每一层只能创建新的投影，不能为了某个 provider 或 Web renderer 就地修改 source。

消息构造入口保持两个并列的语义入口：`InternalMessageFactory` 只生成 `<system_reminder>` 内部消息及其 internal metadata；`UserContentBuilder` 一次性生成普通用户 `HumanMessage` 的全部 content blocks、block metadata 和 `response_metadata.attachments`。两者不互相调用，只共享底层 block 校验、转义和投影基础设施。选择三层投影而不是让 API 直接复用模型 payload，是因为同一条消息同时有三个不同需求：模型需要可发送的 block，checkpoint 需要精确重放，Web 需要安全且轻量的可见内容。`AIMessage` 的现有有序 carrier 和 provider projection 作为用户消息的结构参照，但不把 assistant 的 reasoning 语义复制到附件模型中。

### 2. 统一 block walker，文本与附件分别提取

新增共享的纯业务遍历逻辑，输入可以是字符串或结构化列表，输出至少包括：

- 按原顺序的 canonical blocks；
- 只由 `text`、`input_text`、`output_text` 等可见文本 block 聚合的 `visible_text`；
- 由附件 metadata、manifest block 或稳定 file id 组成的 `attachments`；
- 未知/非文本 rich block 的原始位置和类型摘要，供 provider/display 投影决定如何处理。

该 walker 不能用“列表转 JSON 字符串”作为未知 block 的兜底。未知 block 在 canonical/full 路径保留，在 display 路径不进入 `content`，在 provider 路径由能力矩阵明确保留、转换或跳过。这样 `RolloutHistoryReader._user_message` 与 assistant 的 block 提取遵循相同的原则，但不把两者的展示字段混在一起。消息 role 负责区分 HumanMessage/AIMessage；附件 manifest kind 只负责区分同一个 HumanMessage 内的系统生成附件 block 与用户原文。

### 3. 附件原件与变体使用同一稳定身份

沿用会话附件存储和稳定 `file_id`，把附件物理数据放在当前会话的 `.boxteam/sessions/.../attachments` 范围内。逻辑引用不向模型或 Web 暴露本机绝对路径，而是提供 workspace-relative/session-resolvable path；后端通过稳定身份和统一路径解析器获取绝对路径。

存储记录区分 `original` 和 `preview` 变体。图片 preview 使用 Pillow 等现有图像处理能力，最长边为 `min(512, max(width, height))`，并在写入前检查不放大。原件保存、preview 生成和 preview 读取是三个可诊断阶段：preview 失败不回滚已经成功保存的原件，也不生成空白假 preview。通用文件仍可只有原件和元数据；这不是失败的默认成功值。

附件元数据至少包含 `file_id`、原始文件名、MIME 类型、相对路径、可用变体和各阶段错误状态。`data_url` 不作为附件身份；它只在 canonical user rich block 中表示本次模型输入实际使用的 preview 内容。

### 4. UserContentBuilder 是用户附件的唯一构造入口

在构造 `HumanMessage` 时，`UserContentBuilder` 为每个持久化附件一次性生成 `<attachment ...>` 文本 manifest、可选的 preview rich block 及它们的 block metadata，包含相对路径、文件名、MIME 类型和稳定身份。manifest 的文本转义可以复用项目现有标记生成器的安全规则，但不能调用 `InternalMessageFactory`，也不能生成 `<system_reminder>`、`internal=true` 或内部 prompt metadata。当前仓库的 `<>` 具体消息工厂服务于 `system_reminder` 内部结构消息；这里复用的是结构化生成原则和展示投影思想，不把两个语义入口串接起来。该 manifest 文本是模型可组合现有能力的稳定入口：没有 image/document/audio 直传能力时，模型仍能自行通过已有文件和终端工具处理原件。

manifest text block 和 preview rich block 在 canonical source 中携带通用 block `metadata`，至少标记 `origin`、`kind`、schema version 和关联 `file_id`；`response_metadata.attachments` 是附件清单的权威来源。新的用户消息不额外复制一份 `display_content`，display projection 直接按 block metadata 提取用户原文和附件。provider projection 在发送前剥离这些 canonical-only 字段，只留下目标协议允许的字段。这样不会把“是否是用户消息”和“是否是附件 manifest”混成同一个标志，也不会依赖展示文本反解析附件身份。

不将“当前 provider 能否直接看懂附件”误当成“附件是否存在”。rich block 投影失败时，只跳过该可选 block，manifest 不被移除，并在请求/trace 的投影诊断中记录失败原因。若原件保存或路径解析失败，则按本地代理的快速失败原则返回带会话、file id 和阶段的详细错误。

### 5. canonical rich block 与 provider wire block 分离

canonical 用户 content 使用项目已有的 LangChain/LiteLLM 兼容 block 形状，并保留未知字段；不在 canonical source 中嵌入 `litellm_payload` 或某个 provider 的顶层包装。附件 block 的 canonical `metadata` 只属于 source，不属于 provider wire protocol。`UserContentBuilder` 生成 source 后，provider adapter 只做临时投影；`InternalMessageFactory` 生成的内部字符串消息不进入这个用户附件构造流程。

模型请求时按以下优先级投影：

1. 先由 LiteLLM 处理目标 provider 能统一的 text/image content；
2. 对 Responses 的 `input_text`/`input_image`、Chat Completions 的 `text`/`image_url`、Anthropic Messages 的相应 text/image 形状使用目标 adapter 的最小转换；
3. 根据 `image_input` 等已有能力决定是否发送 preview rich block；不支持时保留 manifest 文本并输出 `not_sent`/`projection_failed` 诊断；
4. 不为 PDF 或其它通用文件建立新的应用 modality。若具体 provider 原生接受 document/file block，adapter 可在请求边界做 MIME 驱动的转换；否则只发送 manifest，source 仍完整保留。

provider 投影结果是短生命周期的请求对象，不回写 `HumanMessage`、checkpoint、rollout 或附件元数据。目标 provider 切换只改变投影，不改变 source。

### 6. checkpoint/history 明确保留 preview base64，但 Web 不拿它当正文

进入 Agent 执行的用户消息按模型输入的 canonical source 保存；如果 source 中有模型使用的 preview `image_url` data URL，就让它随 HumanMessage 进入 checkpoint 和 rollout JSONL。这样历史恢复可以验证 block 顺序和模型输入，而不是将 preview 当成临时 UI 数据丢掉。

SQLite 只建立可定位的消息/content block 坐标、附件身份和有界 display projection，不复制 preview 正文到索引列。完整 LangGraph 恢复从 checkpoint view 读取原始 HumanMessage；目标 provider adapter 再生成临时请求。默认 Web history 则只读取 `visible_text` 与 `TurnAttachmentDTO`，绝不把 full content list 或 data URL 放入 `TurnUserMessageDTO.content`。新用户消息不写重复的 `display_content`；旧消息仍可使用已有 `display_content` 作为兼容 fallback，内部消息继续使用现有的 internal display policy。

这也是修复“显示出 `{"image_url": {"url": "data:image/...`”问题的关键：问题不在于 checkpoint 中存在 base64，而在于用户消息 display projection 把非文本 block 直接字符串化。

### 7. Web 使用附件 DTO 和会话资源入口

沿用用户消息已有的 `content` 与 `attachments` 分离接口。`MessageAttachments` 通过稳定 `file_id` 请求 thumbnail/original，并根据元数据显示图片、通用文件或失败状态。点击卡片时发出会话资源选择动作，由主窗口的右侧侧边栏打开对应附件；该资源归属当前会话，不放入工作区底部面板或 Gateway 左侧导航。

前端不读取或解析 checkpoint content，也不从正文 markdown 反推 file id。缩略图与正文加载可渐进进行：最新 Turn 先展示文本和附件占位，预览随后更新；原件只在用户点击资源时请求。

### 8. 兼容旧数据而不重写历史

- 旧的纯字符串用户消息归一为一个文本 block；旧的结构化列表按 block walker 读取。
- 旧消息中的 `image_url`、`data_url` 和附件 metadata 不做破坏性清洗；展示层开始按 block 提取后，旧 checkpoint 也不再被整体 dump。
- 既有附件 API 的 `original`/`thumbnail` 读取继续复用，preview 默认尺寸迁移到 512；文件名、MIME 和稳定 id 的新字段以追加方式提供。
- 不扫描磁盘重建会话附件身份，不根据显示名或显示文本推导路径；无法通过现有稳定 id 解析的历史附件返回明确错误。
- 迁移期间不改写 rollout JSONL 和 checkpoint。回滚应用代码仍可读取 LangChain-compatible 的用户 content；新增的 projection/DTO 字段缺失时按既有旧历史读取路径处理，但不把新 source 数据迁移成另一种格式。

## Risks / Trade-offs

- [preview base64 增大 checkpoint/rollout] → 只保存最长边不超过 512 的 preview，不把原图塞入 message；SQLite 不复制正文，并在历史 API 上继续使用 projection/detail/full 的边界。
- [不同 provider 对 image/document block 的语义不一致] → canonical source 不绑定单一 wire schema；LiteLLM 处理共通部分，目标 adapter 按能力显式投影，未发送与失败状态可诊断。
- [旧历史存在被整体序列化的用户 content] → reader 先按结构化 block 解析，保留真正字符串为文本；不对无法可靠推断的旧字符串进行启发式 JSON 反解析，避免改变用户原文。
- [预览文件与原件生命周期不一致] → 以稳定 file id 和 variant 记录绑定原件/预览，删除、清理和读取均经过附件存储层；任何缺失 variant 都返回状态而不是空图片。
- [Web 与模型需要不同数据量] → 保持 `content`、`attachments`、full checkpoint 三种边界；Web renderer 永不接触 full content，模型恢复也不依赖 Web DTO。
- [路径暴露工作区内部结构] → 只提供 workspace-relative/session-resolvable path，不暴露绝对路径；服务端对 file id 和相对路径做会话范围校验。
