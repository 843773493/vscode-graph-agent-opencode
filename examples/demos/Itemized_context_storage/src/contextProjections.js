import {
  canonicalJson,
  contentHashFromCanonicalJson,
  sha256,
} from "./canonicalItems.js";

export function createRequestPlan({ view, includedItems, historyHighWatermark, sessionId }) {
  const requestOnly = [
    {
      contribution_id: "system:demo-policy",
      source_kind: "system",
      source_revision: "system-revision-1",
      body: "你是一个只读文件助手。回答时引用工具返回的事实。",
      request_only: true,
    },
    {
      contribution_id: "tools:demo-read-file",
      source_kind: "tool_set",
      source_revision: "tools-revision-1",
      body: { name: "read_file", description: "读取一个 UTF-8 文本文件" },
      request_only: true,
    },
  ].map((entry) => {
    const canonicalBody = canonicalJson(entry.body);
    return {
      ...entry,
      content_hash: contentHashFromCanonicalJson(canonicalBody),
      content_length: Buffer.byteLength(canonicalBody),
    };
  });
  const selection = [
    ...view.map((entry) => ({
      plan_ordinal: entry.view_ordinal,
      selection_kind: entry.selection_kind,
      ref: { ref_type: "canonical_item", ref_id: entry.item_id },
      item_sequence: entry.item_sequence,
      content_hash: entry.content_hash,
      included: Boolean(entry.included),
      omission_reason: entry.omission_reason,
    })),
    ...requestOnly.map((entry, index) => ({
      plan_ordinal: view.length + index,
      selection_kind: "request_only",
      ref: { ref_type: entry.source_kind, ref_id: entry.contribution_id },
      content_hash: entry.content_hash,
      included: true,
      omission_reason: null,
    })),
  ];
  const wireMessages = [
    { role: "system", content: requestOnly[0].body, source: "request_only" },
  ];
  for (const item of includedItems) {
    if (item.semantic_kind === "tool_call") {
      wireMessages.push({
        role: "assistant",
        tool_calls: item.payload.tool_calls,
        source_item_id: item.item_id,
      });
    } else if (item.semantic_kind === "tool_result") {
      wireMessages.push({
        role: "tool",
        tool_call_id: item.payload.tool_call_id,
        content: item.payload.content,
        source_item_id: item.item_id,
      });
    } else {
      wireMessages.push({
        role: item.wire_role ?? item.semantic_kind,
        content: item.payload,
        source_item_id: item.item_id,
      });
    }
  }
  const plan = {
    schema: "context-request-plan:v1",
    plan_id: "plan-demo-001",
    session_id: sessionId,
    view_id: "view-main",
    selection_policy: "active_view",
    source_revision: "demo-revision-1",
    history_high_watermark: historyHighWatermark,
    request_only: requestOnly,
    selection,
    wire_request: {
      provider: "demo-provider",
      target_format: "message-array",
      messages: wireMessages,
    },
  };
  plan.plan_hash = `sha256:jcs:v1:${sha256(canonicalJson(plan))}`;
  return plan;
}

export function createTranscriptProjection({ items, sessionId }) {
  const entries = items.map((record) => ({
    item_id: record.item_id,
    role: record.semantic_kind === "user_input" ? "user" : "assistant",
    text: record.payload,
  }));
  return {
    schema: "transcript-projection:v1",
    session_id: sessionId,
    note: "这是面向用户的粗粒度投影，不是模型上下文的 canonical source。",
    entries,
  };
}
