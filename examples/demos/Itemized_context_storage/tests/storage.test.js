import { afterEach, describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { appendFile, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import {
  canonicalJson,
  contentHash,
  ItemizedDemoStore,
} from "../src/storage.js";
import { resetDemoRuntime } from "../src/runtimePaths.js";

const repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], {
  cwd: process.cwd(),
  encoding: "utf8",
}).trim();
const testRuntimeBasePath = path.join(
  repositoryRoot,
  "out",
  "tests",
  "examples",
  "demos",
  "Itemized_context_storage",
  "tests",
  "storage.test",
  "workspace",
  randomUUID(),
  "runtime",
);

const TEST_RUNTIMES = {
  canonicalItems: "test-suite/canonical",
  requestPlan: "test-suite/request-plan",
  transcript: "test-suite/transcript",
  viewReplacement: "test-suite/view-replacement",
  catalogFailure: "test-suite/catalog-failure",
  concurrentAppend: "test-suite/concurrent-append",
};
const storesToClose = new Set();

function createTestStore(runtimeRelativePath) {
  const store = new ItemizedDemoStore(runtimeRelativePath, {
    runtimeBasePath: testRuntimeBasePath,
  });
  storesToClose.add(store);
  return store;
}

afterEach(() => {
  for (const store of storesToClose) {
    store.close();
  }
  storesToClose.clear();
});

describe("itemized context storage teaching slice", () => {
  test("runtime 相对路径统一识别 POSIX/Windows 分隔符并拒绝越界段", () => {
    const expectedRuntimeRoot = path.join(testRuntimeBasePath, "nested", "runtime");
    expect(new ItemizedDemoStore("nested/runtime", {
      runtimeBasePath: testRuntimeBasePath,
    }).runtimeRoot).toBe(expectedRuntimeRoot);
    expect(new ItemizedDemoStore("nested\\runtime", {
      runtimeBasePath: testRuntimeBasePath,
    }).runtimeRoot).toBe(expectedRuntimeRoot);

    for (const invalidRuntimeName of [
      "../outside",
      "..\\outside",
      "nested/../outside",
      "nested\\..\\outside",
      "",
    ]) {
      expect(() => new ItemizedDemoStore(invalidRuntimeName, {
        runtimeBasePath: testRuntimeBasePath,
      })).toThrow();
    }
  });

  test("重复初始化复用已打开的数据库，reset 前关闭旧句柄", async () => {
    const store = createTestStore("test-suite/reopen");
    let openCount = 0;
    let releaseOpen;
    const openGate = new Promise((resolve) => {
      releaseOpen = resolve;
    });
    const openStore = store.open.bind(store);
    store.open = async () => {
      openCount += 1;
      await openGate;
      return await openStore();
    };
    const initializationOperation = store.initialize();
    try {
      await expect(store.initialize({ reset: true })).rejects.toThrow(
        "不能在初始化进行期间重置 runtime",
      );
    } finally {
      releaseOpen();
    }
    const [firstInitialization, concurrentInitialization] = await Promise.all([
      initializationOperation,
      store.initialize(),
    ]);
    expect(firstInitialization).toBe(store);
    expect(concurrentInitialization).toBe(store);
    expect(openCount).toBe(1);
    const firstDatabase = store.db;

    await store.initialize();
    expect(store.db).toBe(firstDatabase);

    await store.initialize({ reset: true });
    expect(store.db).not.toBe(firstDatabase);
    expect(() => firstDatabase.query("SELECT 1").get()).toThrow();
    expect(store.readCatalog()).toHaveLength(6);
  });

  test("派生 view 创建失败后可用已有 canonical items 重试初始化", async () => {
    const store = createTestStore("test-suite/initialize-retry");
    await store.open();
    store.db.exec(`
      CREATE TRIGGER reject_initial_view
      BEFORE INSERT ON context_views
      BEGIN
        SELECT RAISE(ABORT, 'fixture rejects initial view');
      END;
    `);

    await expect(store.initialize()).rejects.toThrow("fixture rejects initial view");
    expect(store.readCatalog()).toHaveLength(6);
    expect(store.readViewItems()).toHaveLength(0);

    store.db.exec("DROP TRIGGER reject_initial_view");
    await store.initialize();

    expect(store.readViewItems()).toHaveLength(6);
    expect(await Bun.file(store.planPath).exists()).toBe(true);
    const transcript = JSON.parse(await Bun.file(store.transcriptPath).text());
    expect(transcript.entries.map((entry) => entry.role)).toEqual(["user", "assistant"]);
  });

  test("初始化拒绝 catalog 未登记的 JSONL 尾部而不重复追加", async () => {
    const unindexedLine = '{"record_type":"unindexed"}\n';
    const emptyStore = createTestStore("test-suite/unindexed-empty-catalog");
    await emptyStore.open();
    await appendFile(emptyStore.jsonlPath, unindexedLine, "utf8");

    await expect(emptyStore.initialize()).rejects.toThrow(
      "canonical JSONL 与 catalog 尾部不一致",
    );
    expect(emptyStore.readCatalog()).toHaveLength(0);
    expect(await readFile(emptyStore.jsonlPath, "utf8")).toBe(unindexedLine);

    const indexedStore = createTestStore("test-suite/unindexed-tail");
    await indexedStore.initialize();
    const indexedJsonl = await readFile(indexedStore.jsonlPath, "utf8");
    await appendFile(indexedStore.jsonlPath, unindexedLine, "utf8");
    indexedStore.close();

    await expect(indexedStore.initialize()).rejects.toThrow(
      "canonical JSONL 与 catalog 尾部不一致",
    );
    expect(indexedStore.readCatalog()).toHaveLength(6);
    expect(await readFile(indexedStore.jsonlPath, "utf8")).toBe(indexedJsonl + unindexedLine);
  });

  test("resetDemoRuntime 只删除指定 runtime 子树", async () => {
    const runtimeMarker = path.join(testRuntimeBasePath, "path-tests", "windows-style", "remove.txt");
    const siblingMarker = path.join(testRuntimeBasePath, "path-tests", "keep.txt");
    await mkdir(path.dirname(runtimeMarker), { recursive: true });
    await writeFile(runtimeMarker, "remove");
    await writeFile(siblingMarker, "keep");

    await resetDemoRuntime("path-tests\\windows-style", testRuntimeBasePath);

    expect(await Bun.file(runtimeMarker).exists()).toBe(false);
    expect(await readFile(siblingMarker, "utf8")).toBe("keep");
    await expect(resetDemoRuntime("path-tests/../keep", testRuntimeBasePath))
      .rejects.toThrow("runtime 相对路径非法");
    expect(await readFile(siblingMarker, "utf8")).toBe("keep");
  });

  test("canonical item 逐行写入 JSONL，SQLite 只记录可定位的 catalog", async () => {
    const store = createTestStore(TEST_RUNTIMES.canonicalItems);
    await store.initialize({ reset: true });
    const lines = (await readFile(store.jsonlPath, "utf8")).trim().split("\n");
    expect(lines).toHaveLength(6);
    expect(store.readCatalog()).toHaveLength(6);
    expect(store.readCatalog()[0].jsonl_offset).toBe(0);
    expect(store.readCatalog()[1].jsonl_offset).toBeGreaterThan(0);
    expect(store.readCatalog()[0]).not.toHaveProperty("payload");
    expect((await store.readItem("item-tool-result-001")).payload.tool_outcome).toBe("success");
  });

  test("catalog 定位到身份不匹配的 canonical 记录时明确失败", async () => {
    const store = createTestStore("test-suite/catalog-identity-mismatch");
    await store.initialize();
    store.db.query("UPDATE item_catalog SET item_id = ? WHERE item_id = ?").run(
      "catalog-item-alias",
      "item-tool-result-001",
    );

    await expect(store.readItem("catalog-item-alias")).rejects.toThrow(
      "canonical JSONL/catalog item 身份不匹配",
    );
  });

  test("active view 记录 included/omitted，request plan 追踪 item 和 request-only", async () => {
    const store = createTestStore(TEST_RUNTIMES.requestPlan);
    await store.open();
    await store.appendSampleItems();
    store.readCatalog = () => {
      throw new Error("active view 不应读取完整 catalog");
    };
    await store.createActiveView();
    const view = store.readViewItems();
    expect(view).toHaveLength(6);
    expect(view.filter((item) => item.included)).toHaveLength(4);
    expect(view.find((item) => item.item_id === "item-reasoning-001").omission_reason).toContain("reasoning");
    expect(view.find((item) => item.item_id === "item-runtime-notice-001").included).toBe(0);

    await store.buildRequestPlan();
    const plan = JSON.parse(await Bun.file(store.planPath).text());
    for (const requestOnly of plan.request_only) {
      expect(requestOnly.content_hash).toBe(contentHash(requestOnly.body));
      expect(requestOnly.content_length).toBe(Buffer.byteLength(canonicalJson(requestOnly.body)));
    }
    expect(plan.selection).toHaveLength(8);
    expect(plan.selection.filter((entry) => entry.selection_kind === "request_only")).toHaveLength(2);
    expect(plan.wire_request.messages.map((message) => message.role)).toEqual([
      "system", "user", "assistant", "tool", "assistant",
    ]);
    expect(plan.wire_request.messages.some((message) => message.content?.includes("先读取文件"))).toBe(false);
  });

  test("active view 重建失败时保留原有完整 view", async () => {
    const store = createTestStore(TEST_RUNTIMES.viewReplacement);
    await store.initialize({ reset: true });
    const previousView = store.readViewItems();
    store.db.exec(`
      CREATE TRIGGER reject_view_item
      BEFORE INSERT ON context_view_items
      WHEN NEW.view_ordinal = 2
      BEGIN
        SELECT RAISE(ABORT, 'fixture rejects view item');
      END;
    `);

    let failure = null;
    try {
      await store.createActiveView();
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toContain("fixture rejects view item");
    expect(store.readViewItems()).toEqual(previousView);
  });

  test("catalog 事务失败时回滚 JSONL，并允许干净重试", async () => {
    const store = createTestStore(TEST_RUNTIMES.catalogFailure);
    await store.open();
    store.db.exec(`
      CREATE TRIGGER reject_catalog_item
      BEFORE INSERT ON item_catalog
      WHEN NEW.item_sequence = 3
      BEGIN
        SELECT RAISE(ABORT, 'fixture rejects catalog item');
      END;
    `);

    let failure = null;
    try {
      await store.appendSampleItems();
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toContain("fixture rejects catalog item");
    expect(store.readCatalog()).toHaveLength(0);
    expect((await stat(store.jsonlPath)).size).toBe(0);

    store.db.exec("DROP TRIGGER reject_catalog_item");
    await store.appendSampleItems();

    expect(store.readCatalog()).toHaveLength(6);
    expect((await readFile(store.jsonlPath, "utf8")).trim().split("\n")).toHaveLength(6);
  });

  test("同一 store 的并发示例追加共用一次写入", async () => {
    const store = createTestStore(TEST_RUNTIMES.concurrentAppend);
    await store.open();

    const firstAppend = store.appendSampleItems();
    const secondAppend = store.appendSampleItems();
    expect(secondAppend).toBe(firstAppend);
    await Promise.all([firstAppend, secondAppend]);

    expect(store.readCatalog()).toHaveLength(6);
    expect((await readFile(store.jsonlPath, "utf8")).trim().split("\n")).toHaveLength(6);
  });

  test("transcript 是粗粒度 UI 投影，不冒充模型上下文", async () => {
    const store = createTestStore(TEST_RUNTIMES.transcript);
    await store.initialize({ reset: true });
    const runtimeSnapshot = await store.readRuntimeSnapshot();
    const transcript = JSON.parse(await Bun.file(store.transcriptPath).text());
    expect(runtimeSnapshot.runtimeRoot).toBe(store.runtimeRoot);
    expect(runtimeSnapshot.catalog).toHaveLength(6);
    expect(transcript.entries.map((entry) => entry.role)).toEqual(["user", "assistant"]);
    expect(transcript.entries.map((entry) => entry.item_id)).not.toContain("item-reasoning-001");
    expect(transcript.note).toContain("不是模型上下文");

    store.readCatalog = () => {
      throw new Error("transcript 不应读取完整 catalog");
    };
    expect(await store.buildTranscript()).toEqual(transcript);
  });

  test("读取快照时尚未生成的可选投影返回 null", async () => {
    const store = createTestStore("test-suite/missing-projections");
    await store.open();
    await store.appendSampleItems();

    const runtimeSnapshot = await store.readRuntimeSnapshot();

    expect(runtimeSnapshot.plan).toBeNull();
    expect(runtimeSnapshot.transcript).toBeNull();
    expect(runtimeSnapshot.catalog).toHaveLength(6);
    expect(runtimeSnapshot.rolloutJsonl).toContain("item-user-001");
  });

  test("读取快照时损坏的 JSON 投影保留解析错误", async () => {
    const store = createTestStore("test-suite/invalid-projection-json");
    await store.open();
    await store.appendSampleItems();
    await writeFile(store.planPath, "{", "utf8");

    await expect(store.readRuntimeSnapshot()).rejects.toThrow(SyntaxError);
  });
});
