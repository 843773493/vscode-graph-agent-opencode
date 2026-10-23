import { createHash } from "node:crypto";

export function canonicalJson(value) {
  if (typeof value === "string") {
    if (!value.isWellFormed()) {
      throw new TypeError("canonical JSON 字符串包含非法 Unicode");
    }
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("canonical JSON 数值必须有限");
    }
    return JSON.stringify(value);
  }
  if (value === null || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${Array.from(value, canonicalJson).join(",")}]`;
  }
  if (typeof value !== "object" || Object.getPrototypeOf(value) !== Object.prototype) {
    throw new TypeError("canonical JSON 值必须是 JSON 基本值、数组或普通对象");
  }
  return `{${Object.keys(value).sort().map((key) => (
    `${canonicalJson(key)}:${canonicalJson(value[key])}`
  )).join(",")}}`;
}

export function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

export function contentHashFromCanonicalJson(canonicalJsonText) {
  return `sha256:jcs:v1:${sha256(canonicalJsonText)}`;
}

export function contentHash(payload) {
  return contentHashFromCanonicalJson(canonicalJson(payload));
}

function textPreview(record) {
  if (record.payload_kind === "text") {
    return record.payload;
  }
  if (record.semantic_kind === "tool_call") {
    const call = record.payload.tool_calls[0];
    return `${call.name}(${canonicalJson(call.args)})`;
  }
  if (record.semantic_kind === "tool_result") {
    return record.payload.content;
  }
  if (record.payload_kind === "opaque") {
    return "[reasoning body is canonical, but hidden from this projection]";
  }
  return canonicalJson(record.payload);
}

const ITEM_PREVIEW_CODE_POINT_LIMIT = 120;

export function createItemProjectionPreview(record) {
  const previewCodePoints = [];
  for (const codePoint of textPreview(record)) {
    if (previewCodePoints.length === ITEM_PREVIEW_CODE_POINT_LIMIT) {
      const content = previewCodePoints.join("");
      return {
        content,
        previewByteLength: Buffer.byteLength(content, "utf8"),
        previewTruncated: 1,
      };
    }
    previewCodePoints.push(codePoint);
  }
  const content = previewCodePoints.join("");
  return {
    content,
    previewByteLength: Buffer.byteLength(content, "utf8"),
    previewTruncated: 0,
  };
}

function createItem({
  itemSequence,
  itemId,
  semanticKind,
  payloadKind,
  payload,
  producerRef,
  status = "completed",
  turnId = null,
  turnScope = null,
  wireRole = null,
  metadata = {},
  createdAt = "2026-09-20T00:00:00.000Z",
}) {
  return {
    format_version: 2,
    record_type: "item",
    item_sequence: itemSequence,
    item_id: itemId,
    semantic_kind: semanticKind,
    payload_kind: payloadKind,
    status,
    producer_ref: producerRef,
    payload,
    content_hash: contentHash(payload),
    created_at: createdAt,
    metadata,
    ...(turnId ? { turn_id: turnId } : {}),
    ...(turnScope ? { turn_scope: turnScope } : {}),
    ...(wireRole ? { wire_role: wireRole } : {}),
  };
}

export function sampleItems() {
  const turnId = "turn-001";
  return [
    createItem({
      itemSequence: 1,
      itemId: "item-user-001",
      semanticKind: "user_input",
      payloadKind: "text",
      payload: "请读取 README 的标题",
      producerRef: { producer_kind: "user", producer_id: "user-input" },
      turnId,
      turnScope: "turn_root",
      wireRole: "user",
    }),
    createItem({
      itemSequence: 2,
      itemId: "item-reasoning-001",
      semanticKind: "reasoning",
      payloadKind: "opaque",
      payload: {
        protection: { class: "internal", redaction: "projection-hidden" },
        text: "先读取文件，再提取第一行标题。",
      },
      producerRef: {
        producer_kind: "model",
        producer_id: "demo-model",
        invocation_id: "invocation-001",
      },
      turnId,
      turnScope: "turn_member",
      metadata: { visibility: "internal" },
    }),
    createItem({
      itemSequence: 3,
      itemId: "item-tool-call-001",
      semanticKind: "tool_call",
      payloadKind: "structured_content",
      payload: {
        tool_calls: [
          { id: "call-001", name: "read_file", args: { path: "README.md" } },
        ],
      },
      producerRef: {
        producer_kind: "model",
        producer_id: "demo-model",
        invocation_id: "invocation-001",
      },
      turnId,
      turnScope: "turn_member",
      wireRole: "assistant",
    }),
    createItem({
      itemSequence: 4,
      itemId: "item-tool-result-001",
      semanticKind: "tool_result",
      payloadKind: "structured_content",
      payload: {
        tool_call_id: "call-001",
        result_id: "result-001",
        tool_outcome: "success",
        content: "# Itemized Context Demo\n\n这是 README 的标题。",
      },
      producerRef: {
        producer_kind: "tool",
        producer_id: "read_file",
        invocation_id: "call-001",
      },
      turnId,
      turnScope: "turn_member",
      wireRole: "tool",
      metadata: { execution_confirmed: true },
    }),
    createItem({
      itemSequence: 5,
      itemId: "item-assistant-001",
      semanticKind: "assistant_output",
      payloadKind: "text",
      payload: "README 的标题是 `Itemized Context Demo`。",
      producerRef: {
        producer_kind: "model",
        producer_id: "demo-model",
        invocation_id: "invocation-001",
      },
      turnId,
      turnScope: "turn_member",
      wireRole: "assistant",
    }),
    createItem({
      itemSequence: 6,
      itemId: "item-runtime-notice-001",
      semanticKind: "runtime_notice",
      payloadKind: "text",
      payload: "工具调用耗时 12ms",
      producerRef: { producer_kind: "runtime", producer_id: "clock" },
      turnScope: "ambient",
      metadata: { visibility: "internal" },
    }),
  ];
}
