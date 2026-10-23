import { appendFile, mkdir, readFile, readdir, stat, truncate, writeFile } from "node:fs/promises";
import path from "node:path";
import { Database } from "bun:sqlite";
import {
  assertCanonicalJsonlTail,
  readCanonicalItemsByCatalogOffsets,
} from "./canonicalJsonl.js";
import {
  canonicalJson,
  createItemProjectionPreview,
  sampleItems,
} from "./canonicalItems.js";
import { createRequestPlan, createTranscriptProjection } from "./contextProjections.js";
import { resetDemoRuntime, resolveRuntimePath, RUNTIME_ROOT } from "./runtimePaths.js";

export { canonicalJson, contentHash, sampleItems, sha256 } from "./canonicalItems.js";

const SESSION_ID = "session-itemized-teaching";

function jsonFile(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

export class ItemizedDemoStore {
  constructor(runtimeRelativePath = "demo", { runtimeBasePath = RUNTIME_ROOT } = {}) {
    this.runtimeRelativePath = runtimeRelativePath;
    this.runtimeBasePath = path.resolve(runtimeBasePath);
    this.runtimeRoot = resolveRuntimePath(runtimeRelativePath, this.runtimeBasePath);
    this.rolloutRoot = path.join(this.runtimeRoot, "sessions", SESSION_ID, "rollout");
    this.jsonlPath = path.join(this.rolloutRoot, "rollout.jsonl");
    this.indexPath = path.join(this.rolloutRoot, "index.sqlite");
    this.planPath = path.join(this.runtimeRoot, "request-plan.json");
    this.transcriptPath = path.join(this.runtimeRoot, "transcript.json");
    this.db = null;
    this.sampleAppendOperation = null;
    this.initializationOperation = null;
    this.initialized = false;
  }

  async open() {
    if (this.db) {
      return this;
    }
    await mkdir(this.rolloutRoot, { recursive: true });
    const jsonlFile = Bun.file(this.jsonlPath);
    if (!(await jsonlFile.exists())) {
      await writeFile(this.jsonlPath, "", "utf8");
    }
    const database = new Database(this.indexPath);
    try {
      database.exec(`
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
      database.query("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)").run(
        "storage_contract",
        "canonical JSONL is payload source; SQLite is catalog/projection/view state",
      );
    } catch (error) {
      database.close(false);
      throw error;
    }
    this.db = database;
    return this;
  }

  close() {
    if (this.db) {
      this.db.close(false);
      this.db = null;
    }
    this.initialized = false;
  }

  requireDb() {
    if (!this.db) {
      throw new Error("ItemizedDemoStore 尚未 open()");
    }
    return this.db;
  }

  executeStatement(sql, ...params) {
    return this.requireDb().query(sql).run(...params);
  }

  queryOne(sql, ...params) {
    return this.requireDb().query(sql).get(...params);
  }

  queryMany(sql, ...params) {
    return this.requireDb().query(sql).all(...params);
  }

  appendSampleItems() {
    if (this.sampleAppendOperation) {
      return this.sampleAppendOperation;
    }
    const appendOperation = this.#appendSampleItems();
    const trackedOperation = appendOperation.finally(() => {
      if (this.sampleAppendOperation === trackedOperation) {
        this.sampleAppendOperation = null;
      }
    });
    this.sampleAppendOperation = trackedOperation;
    return trackedOperation;
  }

  async #appendSampleItems() {
    if (this.queryOne("SELECT item_id FROM item_catalog LIMIT 1")) {
      throw new Error("当前 demo runtime 已有数据，请使用 --reset 建立新的教学快照");
    }

    const sampleEntries = sampleItems().map((item) => {
      const jsonlLine = Buffer.from(`${canonicalJson(item)}\n`, "utf8");
      const payloadText = canonicalJson(item.payload);
      return {
        item,
        jsonlLine,
        payloadLength: Buffer.byteLength(payloadText),
        projection: createItemProjectionPreview(item),
      };
    });
    const db = this.requireDb();
    const insertCatalog = db.query(
      `INSERT INTO item_catalog (
        item_sequence, item_id, semantic_kind, payload_kind, status, turn_id,
        turn_scope, wire_role, producer_ref_json, payload_length, source_revision,
        content_hash, jsonl_offset, jsonl_length, created_at, metadata_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    const insertProjection = db.query(
      `INSERT INTO item_projections (
        item_sequence, item_id, content, content_length, content_truncated, projection_version
      ) VALUES (?, ?, ?, ?, ?, ?)`,
    );
    const appendCatalogBatch = db.transaction(() => {
      let nextJsonlOffset = initialJsonlOffset;
      for (const entry of sampleEntries) {
        const { item, jsonlLine, payloadLength, projection } = entry;
        insertCatalog.run(
          item.item_sequence,
          item.item_id,
          item.semantic_kind,
          item.payload_kind,
          item.status,
          item.turn_id ?? null,
          item.turn_scope ?? null,
          item.wire_role ?? null,
          canonicalJson(item.producer_ref),
          payloadLength,
          item.producer_ref.source_version ?? "demo-revision-1",
          item.content_hash,
          nextJsonlOffset,
          jsonlLine.byteLength,
          item.created_at,
          canonicalJson(item.metadata),
        );
        insertProjection.run(
          item.item_sequence,
          item.item_id,
          projection.content,
          projection.previewByteLength,
          projection.previewTruncated,
          1,
        );
        nextJsonlOffset += jsonlLine.byteLength;
      }
    });
    const initialJsonlOffset = (await stat(this.jsonlPath)).size;
    try {
      await appendFile(this.jsonlPath, Buffer.concat(sampleEntries.map((entry) => entry.jsonlLine)));
      appendCatalogBatch();
    } catch (error) {
      try {
        await truncate(this.jsonlPath, initialJsonlOffset);
      } catch (rollbackError) {
        throw new AggregateError(
          [error, rollbackError],
          `canonical JSONL 追加失败且无法回滚到 offset=${initialJsonlOffset}`,
        );
      }
      throw error;
    }
  }

  readCatalog() {
    return this.queryMany(`
      SELECT item_sequence, item_id, semantic_kind, payload_kind, status,
        turn_id, turn_scope, wire_role, payload_length, source_revision,
        content_hash, jsonl_offset, jsonl_length, created_at
      FROM item_catalog ORDER BY item_sequence
    `);
  }

  readViewItems(viewId = "view-main") {
    return this.queryMany(`
      SELECT v.view_ordinal, v.item_id, v.included, v.omission_reason,
        v.selection_kind, c.item_sequence, c.semantic_kind, c.payload_kind,
        c.content_hash, c.jsonl_offset, c.jsonl_length
      FROM context_view_items v
      JOIN item_catalog c ON c.item_id = v.item_id
      WHERE v.view_id = ? ORDER BY v.view_ordinal
    `, viewId);
  }

  async readItem(itemId) {
    const catalogEntry = this.queryOne(
      "SELECT item_id, item_sequence, jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
      itemId,
    );
    if (!catalogEntry) {
      throw new Error(`找不到 item_catalog: ${itemId}`);
    }
    return (await readCanonicalItemsByCatalogOffsets(this.jsonlPath, [catalogEntry]))[0];
  }

  async createActiveView() {
    const viewId = "view-main";
    const db = this.requireDb();
    const deleteViewItems = db.query("DELETE FROM context_view_items WHERE view_id = ?");
    const deleteView = db.query("DELETE FROM context_views WHERE view_id = ?");
    const insertView = db.query(
      "INSERT INTO context_views (view_id, view_kind, source_revision, active, created_at) VALUES (?, ?, ?, ?, ?)",
    );
    const insertViewItems = db.query(`
      INSERT INTO context_view_items (
        view_id, item_id, view_ordinal, included, omission_reason, selection_kind
      )
      SELECT ?, item_id, item_sequence - 1,
        CASE
          WHEN semantic_kind IN ('runtime_notice', 'reasoning') THEN 0
          ELSE 1
        END,
        CASE semantic_kind
          WHEN 'reasoning' THEN 'demo provider projection hides internal reasoning'
          WHEN 'runtime_notice' THEN 'ambient runtime notice is not canonical model history'
          ELSE NULL
        END,
        'canonical_history'
      FROM item_catalog
    `);
    db.transaction(() => {
      deleteViewItems.run(viewId);
      deleteView.run(viewId);
      insertView.run(
        viewId,
        "active_view",
        "demo-revision-1",
        1,
        "2026-09-20T00:00:00.000Z",
      );
      insertViewItems.run(viewId);
    })();
    return viewId;
  }

  async buildRequestPlan() {
    const view = this.readViewItems();
    const includedItems = await readCanonicalItemsByCatalogOffsets(
      this.jsonlPath,
      view.filter((candidate) => candidate.included),
    );
    const historyHighWatermark = this.queryOne(
      "SELECT COALESCE(MAX(item_sequence), 0) AS high_watermark FROM item_catalog",
    ).high_watermark;
    const plan = createRequestPlan({
      view,
      includedItems,
      historyHighWatermark,
      sessionId: SESSION_ID,
    });
    await writeFile(this.planPath, jsonFile(plan), "utf8");
    return plan;
  }

  async buildTranscript() {
    const transcriptCatalogEntries = this.queryMany(`
      SELECT item_sequence, item_id, jsonl_offset, jsonl_length
      FROM item_catalog
      WHERE semantic_kind IN (?, ?)
      ORDER BY item_sequence
    `, "user_input", "assistant_output");
    const transcriptItems = await readCanonicalItemsByCatalogOffsets(
      this.jsonlPath,
      transcriptCatalogEntries,
    );
    const transcript = createTranscriptProjection({ items: transcriptItems, sessionId: SESSION_ID });
    await writeFile(this.transcriptPath, jsonFile(transcript), "utf8");
    return transcript;
  }

  async #assertJsonlTailMatchesCatalog() {
    const lastCatalogEntry = this.queryOne(
      "SELECT jsonl_offset, jsonl_length FROM item_catalog ORDER BY item_sequence DESC LIMIT 1",
    );
    const indexedEnd = lastCatalogEntry
      ? lastCatalogEntry.jsonl_offset + lastCatalogEntry.jsonl_length
      : 0;
    await assertCanonicalJsonlTail(this.jsonlPath, indexedEnd);
  }

  async initialize({ reset = false } = {}) {
    if (this.initializationOperation) {
      if (reset) {
        throw new Error("不能在初始化进行期间重置 runtime");
      }
      return this.initializationOperation;
    }

    const initializationOperation = this.#initialize({ reset });
    const trackedOperation = initializationOperation.finally(() => {
      if (this.initializationOperation === trackedOperation) {
        this.initializationOperation = null;
      }
    });
    this.initializationOperation = trackedOperation;
    return trackedOperation;
  }

  async #initialize({ reset }) {
    if (reset) {
      this.close();
      await resetDemoRuntime(this.runtimeRelativePath, this.runtimeBasePath);
    }
    await this.open();
    await this.#assertJsonlTailMatchesCatalog();
    if (this.initialized) {
      return this;
    }
    if (!this.queryOne("SELECT item_id FROM item_catalog LIMIT 1")) {
      await this.appendSampleItems();
    }
    await this.createActiveView();
    await this.buildRequestPlan();
    await this.buildTranscript();
    this.initialized = true;
    return this;
  }

  async readRuntimeSnapshot() {
    const readJson = async (filePath) => {
      try {
        return JSON.parse(await readFile(filePath, "utf8"));
      } catch (error) {
        if (error?.code === "ENOENT") {
          return null;
        }
        throw error;
      }
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
