import "fake-indexeddb/auto";
import { describe, expect, test } from "bun:test";
import { browserViewKey, readBrowserView, updateBrowserView, updateBrowserViewCache } from "./browserViewStore";

describe("浏览器视图 IndexedDB", () => {
  test("Gateway、工作区和 Thread 的相同显示名不影响隔离，删除后真实存储为空", async () => {
    const first = browserViewKey("gateway-a", "draft", "workspace", "session", "thread-a");
    const otherThread = browserViewKey("gateway-a", "draft", "workspace", "session", "thread-b");
    const otherGateway = browserViewKey("gateway-b", "draft", "workspace", "session", "thread-a");
    await updateBrowserView(first, () => "未发送内容");
    expect(await readBrowserView<string>(first)).toBe("未发送内容");
    expect(await readBrowserView(otherThread)).toBeUndefined();
    expect(await readBrowserView(otherGateway)).toBeUndefined();
    await updateBrowserView(first, () => undefined);
    expect(await readBrowserView<string>(first)).toBeUndefined();
  });
  test("并发读改写按事务串行，不能丢失其他窗口的变更", async () => {
    const key = browserViewKey("gateway-concurrent", "settings");
    await Promise.all(Array.from({ length: 20 }, () => updateBrowserView<number>(key, value => (value ?? 0) + 1)));
    expect(await readBrowserView<number>(key)).toBe(20);
  });
  test("更新失败回滚，不返回成功也不覆盖原值", async () => {
    const key = browserViewKey("gateway-rollback", "settings");
    await updateBrowserView(key, () => "原值");
    await expect(updateBrowserView(key, () => { throw new Error("拒绝非法输入"); })).rejects.toThrow("拒绝非法输入");
    expect(await readBrowserView<string>(key)).toBe("原值");
  });
  test("缓存正文与索引事务一起提交，容量淘汰不会删除其他展示设置", async () => {
    const index = "cache-atomic-index", settings = "cache-atomic-settings";
    const limits = { entries: 2, bytes: 10 };
    await updateBrowserView(settings, () => "保留设置");
    await Promise.all([1, 2].map(id => updateBrowserViewCache(`cache-atomic-${id}`, index,
      () => ({ bytes: 4, savedAt: id, text: `正文${id}` }), limits)));
    await expect(updateBrowserViewCache("cache-atomic-1", index, () => { throw new Error("拒绝写入"); }, limits)).rejects.toThrow("拒绝写入");
    expect(await readBrowserView<{ bytes: number; savedAt: number; text: string }>("cache-atomic-1")).toEqual({ bytes: 4, savedAt: 1, text: "正文1" });
    await updateBrowserViewCache("cache-atomic-3", index, () => ({ bytes: 5, savedAt: 3, text: "正文3" }), limits);
    expect(await readBrowserView("cache-atomic-1")).toBeUndefined();
    expect(await readBrowserView<Record<string, unknown>>(index)).toEqual({
      "cache-atomic-2": { bytes: 4, savedAt: 2 }, "cache-atomic-3": { bytes: 5, savedAt: 3 },
    });
    expect(await readBrowserView<string>(settings)).toBe("保留设置");
  });
});

test("旧草稿只迁移当前身份，保留原件且不覆盖已经存在的本地输入", async () => {
  const { migrateComposerDrafts, readComposerDraft } = await import("./composerDrafts/storage");
  const previous = Object.getOwnPropertyDescriptor(globalThis, "window");
  const storage = new Map([["boxteam.web.composerDrafts", JSON.stringify({
    "user:a::workspace::session::thread:a": "原草稿", "user:b::workspace::session::thread:a": "其他用户的草稿",
  })]]);
  Object.defineProperty(globalThis, "window", { configurable: true, value: { localStorage: {
    getItem: (key: string) => storage.get(key) ?? null, removeItem: (key: string) => storage.delete(key),
  } } });
  try {
    await updateBrowserView("draft:migration-gateway::workspace::session::thread:a", () => "本地新草稿");
    await migrateComposerDrafts("migration-gateway", "a");
    expect(await readComposerDraft("migration-gateway::workspace::session::thread:a")).toBe("本地新草稿");
    expect(await readBrowserView<Record<string, string>>("legacy-drafts:migration-gateway")).toEqual({
      "user:a::workspace::session::thread:a": "原草稿", "user:b::workspace::session::thread:a": "其他用户的草稿",
    });
    expect(storage.has("boxteam.web.composerDrafts")).toBe(false);
  } finally {
    if (previous) Object.defineProperty(globalThis, "window", previous); else Reflect.deleteProperty(globalThis, "window");
  }
});
