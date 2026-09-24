import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, rm, stat, writeFile, appendFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Database } from "bun:sqlite";

const DEMO_ROOT = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const RUNTIME_ROOT = path.join(DEMO_ROOT, "runtime");
const SESSION_ID = "session-itemized-teaching";

function assertRuntimeName(runtimeName) {
  if (!/^[A-Za-z0-9._-]+$/.test(runtimeName)) {
    throw new Error(`runtime 名称非法: ${runtimeName}`);
  }
}

function sortJson(value) {
  if (Array.isArray(value)) {
    return value.map(sortJson);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, sortJson(child)]),
    );
  }
  return value;
}

export function canonicalJson(value) {
  return JSON.stringify(sortJson(value));
}

export function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

export function contentHash(payload) {
  return `sha256:jcs:v1:${sha256(canonicalJson(payload))}`;
}

function jsonFile(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
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

function projectionContent(record) {
  const content = textPreview(record);
  return {
    content: content.slice(0, 120),
    contentLength: content.length,
    contentTruncated: content.length > 120 ? 1 : 0,
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

export class ItemizedDemoStore {
  constructor(runtimeName = "demo") {
    assertRuntimeName(runtimeName);
    this.runtimeName = runtimeName;
    this.runtimeRoot = path.join(RUNTIME_ROOT, runtimeName);
    this.rolloutRoot = path.join(this.runtimeRoot, "sessions", SESSION_ID, "rollout");
    this.jsonlPath = path.join(this.rolloutRoot, "rollout.jsonl");
    this.indexPath = path.join(this.rolloutRoot, "index.sqlite");
    this.planPath = path.join(this.runtimeRoot, "request-plan.json");
    this.transcriptPath = path.join(this.runtimeRoot, "transcript.json");
    this.db = null;
  }

  async open() {
    await mkdir(this.rolloutRoot, { recursive: true });
    const jsonlFile = Bun.file(this.jsonlPath);
    if (!(await jsonlFile.exists())) {
      await writeFile(this.jsonlPath, "", "utf8");
    }
    this.db = new Database(this.indexPath);
    this.db.exec(`
      PRAGMA foreign_keys = ON;
      CREATE TABLE IF NOT EXISTS item_catalog (
        item_sequence INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL UNIQUE,
        semantic_kind TEXT NOT NULL,
        payload_kind TEXT NOT NULL,
        status TEXT NOT NULL,
        turn_id TEXT,
        turn_scope TEXT,
        wire_role TEXT,
        producer_ref_json TEXT NOT NULL,
        payload_length INTEGER NOT NULL,
        source_revision TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        jsonl_offset INTEGER NOT NULL,
        jsonl_length INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS item_projections (
        item_sequence INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        content_length INTEGER NOT NULL,
        content_truncated INTEGER NOT NULL,
        projection_version INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS context_views (
        view_id TEXT PRIMARY KEY,
        view_kind TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        active INTEGER NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS context_view_items (
        view_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        view_ordinal INTEGER NOT NULL,
        included INTEGER NOT NULL,
        omission_reason TEXT,
        selection_kind TEXT NOT NULL,
        PRIMARY KEY (view_id, item_id)
      );
      CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
    `);
    this.run(
      "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
      "storage_contract",
      "canonical JSONL is payload source; SQLite is catalog/projection/view state",
    );
    return this;
  }

  close() {
    if (this.db) {
      this.db.close(false);
      this.db = null;
    }
  }

  requireDb() {
    if (!this.db) {
      throw new Error("ItemizedDemoStore 尚未 open()");
    }
    return this.db;
  }

  run(sql, ...params) {
    return this.requireDb().query(sql).run(...params);
  }

  get(sql, ...params) {
    return this.requireDb().query(sql).get(...params);
  }

  all(sql, ...params) {
    return this.requireDb().query(sql).all(...params);
  }

  async appendItem(item) {
    if (this.get("SELECT item_id FROM item_catalog WHERE item_id = ?", item.item_id)) {
      throw new Error(`item_id 已存在，拒绝重复追加: ${item.item_id}`);
    }
    const rawLine = `${canonicalJson(item)}\n`;
    const currentSize = (await stat(this.jsonlPath)).size;
    await appendFile(this.jsonlPath, rawLine, "utf8");
    const payloadText = canonicalJson(item.payload);
    const projection = projectionContent(item);
    this.run(
      `INSERT INTO item_catalog (
        item_sequence, item_id, semantic_kind, payload_kind, status, turn_id,
        turn_scope, wire_role, producer_ref_json, payload_length, source_revision,
        content_hash, jsonl_offset, jsonl_length, created_at, metadata_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      item.item_sequence,
      item.item_id,
      item.semantic_kind,
      item.payload_kind,
      item.status,
      item.turn_id ?? null,
      item.turn_scope ?? null,
      item.wire_role ?? null,
      canonicalJson(item.producer_ref),
      Buffer.byteLength(payloadText),
      item.producer_ref.source_version ?? "demo-revision-1",
      item.content_hash,
      currentSize,
      Buffer.byteLength(rawLine),
      item.created_at,
      canonicalJson(item.metadata),
    );
    this.run(
      `INSERT INTO item_projections (
        item_sequence, item_id, content, content_length, content_truncated, projection_version
      ) VALUES (?, ?, ?, ?, ?, ?)`,
      item.item_sequence,
      item.item_id,
      projection.content,
      projection.contentLength,
      projection.contentTruncated,
      1,
    );
    return item;
  }

  async appendSampleItems() {
    if (this.get("SELECT item_id FROM item_catalog LIMIT 1")) {
      throw new Error("当前 demo runtime 已有数据，请使用 --reset 建立新的教学快照");
    }
    for (const item of sampleItems()) {
      await this.appendItem(item);
    }
  }

  readCatalog() {
    return this.all(`
      SELECT item_sequence, item_id, semantic_kind, payload_kind, status,
        turn_id, turn_scope, wire_role, payload_length, source_revision,
        content_hash, jsonl_offset, jsonl_length, created_at
      FROM item_catalog ORDER BY item_sequence
    `);
  }

  readViewItems(viewId = "view-main") {
    return this.all(`
      SELECT v.view_ordinal, v.item_id, v.included, v.omission_reason,
        v.selection_kind, c.item_sequence, c.semantic_kind, c.payload_kind,
        c.content_hash, c.jsonl_offset, c.jsonl_length
      FROM context_view_items v
      JOIN item_catalog c ON c.item_id = v.item_id
      WHERE v.view_id = ? ORDER BY v.view_ordinal
    `, viewId);
  }

  async readItem(itemId) {
    const catalog = this.get(
      "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
      itemId,
    );
    if (!catalog) {
      throw new Error(`找不到 item_catalog: ${itemId}`);
    }
    const bytes = await readFile(this.jsonlPath);
    const line = bytes.subarray(catalog.jsonl_offset, catalog.jsonl_offset + catalog.jsonl_length);
    return JSON.parse(line.toString("utf8"));
  }

  async createActiveView() {
    const viewId = "view-main";
    this.run("DELETE FROM context_view_items WHERE view_id = ?", viewId);
    this.run("DELETE FROM context_views WHERE view_id = ?", viewId);
    this.run(
      "INSERT INTO context_views (view_id, view_kind, source_revision, active, created_at) VALUES (?, ?, ?, ?, ?)",
      viewId,
      "active_view",
      "demo-revision-1",
      1,
      "2026-09-20T00:00:00.000Z",
    );
    for (const item of this.readCatalog()) {
      const included = item.semantic_kind !== "runtime_notice" && item.semantic_kind !== "reasoning";
      const omissionReason = item.semantic_kind === "reasoning"
        ? "demo provider projection hides internal reasoning"
        : item.semantic_kind === "runtime_notice"
          ? "ambient runtime notice is not canonical model history"
          : null;
      this.run(
        `INSERT INTO context_view_items (
          view_id, item_id, view_ordinal, included, omission_reason, selection_kind
        ) VALUES (?, ?, ?, ?, ?, ?)`,
        viewId,
        item.item_id,
        item.item_sequence - 1,
        included ? 1 : 0,
        omissionReason,
        "canonical_history",
      );
    }
    return viewId;
  }

  async buildRequestPlan() {
    const view = this.readViewItems();
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
    ].map((entry) => ({
      ...entry,
      content_hash: contentHash(entry.body),
      content_length: Buffer.byteLength(canonicalJson(entry.body)),
    }));
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
    for (const entry of view.filter((candidate) => candidate.included)) {
      const item = await this.readItem(entry.item_id);
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
      session_id: SESSION_ID,
      view_id: "view-main",
      selection_policy: "active_view",
      source_revision: "demo-revision-1",
      history_high_watermark: Math.max(...this.readCatalog().map((item) => item.item_sequence)),
      request_only: requestOnly,
      selection,
      wire_request: {
        provider: "demo-provider",
        target_format: "message-array",
        messages: wireMessages,
      },
    };
    plan.plan_hash = `sha256:jcs:v1:${sha256(canonicalJson(plan))}`;
    await writeFile(this.planPath, jsonFile(plan), "utf8");
    return plan;
  }

  async buildTranscript() {
    const entries = [];
    for (const item of this.readCatalog()) {
      if (!["user_input", "assistant_output"].includes(item.semantic_kind)) {
        continue;
      }
      const record = await this.readItem(item.item_id);
      entries.push({
        item_id: item.item_id,
        role: item.semantic_kind === "user_input" ? "user" : "assistant",
        text: record.payload,
      });
    }
    const transcript = {
      schema: "transcript-projection:v1",
      session_id: SESSION_ID,
      note: "这是面向用户的粗粒度投影，不是模型上下文的 canonical source。",
      entries,
    };
    await writeFile(this.transcriptPath, jsonFile(transcript), "utf8");
    return transcript;
  }

  async initialize({ reset = false } = {}) {
    if (reset) {
      await resetDemoRuntime(this.runtimeName);
    }
    await this.open();
    if (!this.get("SELECT item_id FROM item_catalog LIMIT 1")) {
      await this.appendSampleItems();
      await this.createActiveView();
      await this.buildRequestPlan();
      await this.buildTranscript();
    }
    return this;
  }

  async inspect() {
    const readJson = async (filePath) => {
      const file = Bun.file(filePath);
      return (await file.exists()) ? JSON.parse(await file.text()) : null;
    };
    const files = [];
    const walk = async (directory) => {
      for (const entry of await readdir(directory, { withFileTypes: true })) {
        const entryPath = path.join(directory, entry.name);
        if (entry.isDirectory()) {
          await walk(entryPath);
        } else {
          const details = await stat(entryPath);
          files.push({
            path: path.relative(this.runtimeRoot, entryPath),
            bytes: details.size,
          });
        }
      }
    };
    await walk(this.runtimeRoot);
    return {
      runtimeRoot: this.runtimeRoot,
      files,
      rolloutJsonl: await readFile(this.jsonlPath, "utf8"),
      catalog: this.readCatalog(),
      view: this.readViewItems(),
      plan: await readJson(this.planPath),
      transcript: await readJson(this.transcriptPath),
    };
  }
}

export async function resetDemoRuntime(runtimeName = "demo") {
  assertRuntimeName(runtimeName);
  const runtimePath = path.join(RUNTIME_ROOT, runtimeName);
  if (!runtimePath.startsWith(`${RUNTIME_ROOT}${path.sep}`)) {
    throw new Error(`拒绝清理 demo 目录之外的路径: ${runtimePath}`);
  }
  await rm(runtimePath, { recursive: true, force: true });
}

export { DEMO_ROOT, RUNTIME_ROOT, SESSION_ID };
