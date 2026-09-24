import { afterEach, describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import { ItemizedDemoStore, resetDemoRuntime } from "../src/storage.js";

const runtimeNames = ["test-canonical", "test-plan", "test-transcript"];

afterEach(async () => {
  await Promise.all(runtimeNames.map((runtimeName) => resetDemoRuntime(runtimeName)));
});

describe("itemized context storage teaching slice", () => {
  test("canonical item 逐行写入 JSONL，SQLite 只记录可定位的 catalog", async () => {
    const store = new ItemizedDemoStore("test-canonical");
    await store.initialize({ reset: true });
    const lines = (await readFile(store.jsonlPath, "utf8")).trim().split("\n");
    expect(lines).toHaveLength(6);
    expect(store.readCatalog()).toHaveLength(6);
    expect(store.readCatalog()[0].jsonl_offset).toBe(0);
    expect(store.readCatalog()[1].jsonl_offset).toBeGreaterThan(0);
    expect(store.readCatalog()[0]).not.toHaveProperty("payload");
    expect((await store.readItem("item-tool-result-001")).payload.tool_outcome).toBe("success");
    store.close();
  });

  test("active view 记录 included/omitted，request plan 追踪 item 和 request-only", async () => {
    const store = new ItemizedDemoStore("test-plan");
    await store.initialize({ reset: true });
    const view = store.readViewItems();
    expect(view).toHaveLength(6);
    expect(view.filter((item) => item.included)).toHaveLength(4);
    expect(view.find((item) => item.item_id === "item-reasoning-001").omission_reason).toContain("reasoning");
    expect(view.find((item) => item.item_id === "item-runtime-notice-001").included).toBe(0);

    const plan = JSON.parse(await Bun.file(store.planPath).text());
    expect(plan.selection).toHaveLength(8);
    expect(plan.selection.filter((entry) => entry.selection_kind === "request_only")).toHaveLength(2);
    expect(plan.wire_request.messages.map((message) => message.role)).toEqual([
      "system", "user", "assistant", "tool", "assistant",
    ]);
    expect(plan.wire_request.messages.some((message) => message.content?.includes("先读取文件"))).toBe(false);
    store.close();
  });

  test("transcript 是粗粒度 UI 投影，不冒充模型上下文", async () => {
    const store = new ItemizedDemoStore("test-transcript");
    await store.initialize({ reset: true });
    const transcript = JSON.parse(await Bun.file(store.transcriptPath).text());
    expect(transcript.entries.map((entry) => entry.role)).toEqual(["user", "assistant"]);
    expect(transcript.entries.map((entry) => entry.item_id)).not.toContain("item-reasoning-001");
    expect(transcript.note).toContain("不是模型上下文");
    store.close();
  });
});
