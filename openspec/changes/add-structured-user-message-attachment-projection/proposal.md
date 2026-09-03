## Why

当前用户消息在历史投影阶段仍可能把包含 `image_url`、预览 base64 或其它 provider block 的结构化 content 直接 `json.dumps`，导致前端把内部传输结构当成用户可见文本展示。用户消息与 `AIMessage` 一样需要按 block 处理：模型请求历史保留真实的有序 content，Web 历史则从 block 和附件元数据生成独立的文本、预览和资源投影。

同时，图片、视频、音频及文档类附件需要共享一套接收、持久化、预览和 provider 投影边界。所有模型请求最终都必须经过 LiteLLM，由 LiteLLM 负责 provider SDK 和协议 wire format 的转换；不支持某种灵活 block 的 provider 仍不会破坏 checkpoint、相对附件路径或前端附件展示。

## What Changes

- 新增结构化用户消息与附件生命周期能力：接收原始附件、按会话持久化原文件、生成受界限的预览变体，并保留稳定附件引用和 MIME/文件名元数据。
- 用户消息 canonical content 改为可按顺序遍历的 block 序列；文本、附件说明和灵活 provider block 不再在历史展示层被整体字符串化。
- 保持 `InternalMessageFactory` 只生成带 `system_reminder` 语义的内部消息；由独立的 `UserContentBuilder` 一次性生成普通用户消息的全部 content blocks 和附件索引，两者共享底层 block 校验与投影基础设施但不互相调用。
- checkpoint、rollout 和模型恢复保留模型实际使用的用户 content，包括为视觉模型生成的预览 `image_url` base64；Web 展示不直接渲染这些原始 block，而是从 block/metadata 生成可见文本和附件列表。
- 为 Chat Completions、Responses、Anthropic Messages 等协议建立统一经过 LiteLLM 的用户 content 请求路径：Chat Completions 与 Anthropic Messages 通过 `litellm.acompletion` 发送，Responses 通过 `litellm.aresponses` 发送；图片预览 block 在目标能力支持时发送，不支持时保留附件路径与明确的投影结果。
- **BREAKING**：应用层不得使用 `langchain-anthropic`、`ChatAnthropic` 或 Anthropic 原生 HTTP 客户端完成最终发送。provider-specific mapper 只能把 canonical content 投影为 LiteLLM 可接受的请求输入并归一化结果，不能绕过 LiteLLM 直连 provider SDK；Anthropic 原生 `image`/`source` wire block 只能由 LiteLLM 内部生成。
- 附件元信息向模型提供工作区内的相对路径和附件专用 manifest 标记；该 manifest 可以复用现有标记生成器的转义约定，但不复用只服务于 `system_reminder` 内部消息的 middleware。系统不新增 `attachment.xxx` 工具或为 PDF 单独引入应用级 modality，模型可组合现有文件、终端和查看能力自行处理未直接注入上下文的附件。
- Web 历史为附件提供独立预览与资源入口；用户点击附件后，在主窗口的右侧侧边栏打开对应会话资源，而不是显示原始 `image_url` JSON。
- 增加混合 block、超大附件、provider 不支持灵活 block 和前端附件资源跳转的确定性验证。

## Capabilities

### New Capabilities

- `structured-user-message-attachments`: 定义用户消息 block、附件存储/预览、模型相对路径元信息、checkpoint 保留和 provider 投影边界。

### Modified Capabilities

- `litellm-aimessage-content-adapter`: 将现有只面向 assistant 的有序 content 与目标 provider 投影要求扩展到用户 content，同时保持 canonical source 不可变。
- `session-turn-history`: 要求用户历史按 block 提取可见文本和附件，禁止把结构化用户 content 整体 dump 为展示文本。
- `rollout-checkpoint-storage`: 明确用户消息的 canonical content、预览 base64 与附件元数据如何在 rollout/checkpoint 中无损保存和恢复。
- `progressive-turn-rendering`: 要求 Web 将用户文本、附件预览和右侧资源入口作为独立展示投影处理。

## Impact

- 后端消息与历史：`MessageService`、`RolloutHistoryReader`、`AttachmentContentService`、`SessionAttachmentStore`、checkpoint/rollout projection 以及相关 DTO/schema。
- Provider 边界：LiteLLM content adapter、Chat Completions、Responses、Anthropic Messages 和能力路由；需区分 canonical 用户 block、LiteLLM 请求输入和由 LiteLLM 生成的目标 provider wire block。应用层不得保留 Anthropic 直连发送路径。
- Web：Composer 附件提交、用户消息渲染、附件预览加载、会话资源/主窗口右侧侧边栏跳转。
- 数据与接口：会话 `.boxteam/sessions/<稳定会话定位>/attachments` 下的原件和变体、历史 API 的用户消息/附件字段、checkpoint 与 rollout JSONL 的结构化 content。
- 不新增模型工具类型，不要求将 PDF、文档或其它 MIME 类型提升为新的应用级媒体枚举；具体 provider 的可发送 block 由投影器和能力声明决定。
