import { describe, expect, test } from "bun:test";
import {
  failedDirectoryEntry,
  loadedDirectoryEntry,
  loadingDirectoryEntry,
  markDirectoryStale,
  pruneDirectoryCache,
  restoreDirectoriesInOrder,
  runWithConcurrency,
  type DirectoryCacheEntry,
} from "./workspaceFileTreeCache";

function entry(lastAccessedAt: number, itemCount = 1): DirectoryCacheEntry {
  return {
    items: Array.from({ length: itemCount }, (_, index) => ({
      name: `file-${index}`,
      path: `file-${index}`,
      kind: "file" as const,
      has_children: false,
      size: 1,
      modified_at: null,
    })),
    loading: false,
    error: null,
    truncated: false,
    nextCursor: null,
    stale: false,
    lastAccessedAt,
  };
}

describe("文件树目录缓存", () => {
  test("加载中条目继承旧快照但不保留错误与过期标记", () => {
    const previous: DirectoryCacheEntry = {
      ...entry(1, 2),
      error: "旧错误",
      stale: true,
      truncated: true,
      nextCursor: "cursor-1",
    };

    expect(loadingDirectoryEntry(previous, 7)).toEqual({
      items: previous.items,
      loading: true,
      error: null,
      truncated: true,
      nextCursor: "cursor-1",
      stale: true,
      lastAccessedAt: 7,
    });
    expect(loadingDirectoryEntry(undefined, 7)).toEqual({
      items: [],
      loading: true,
      error: null,
      truncated: false,
      nextCursor: null,
      stale: false,
      lastAccessedAt: 7,
    });
  });

  test("后端快照条目清空错误与过期标记并允许覆盖条目", () => {
    const result = { items: entry(0, 2).items, truncated: true, next_cursor: "cursor-2" };
    expect(loadedDirectoryEntry(result, 9)).toEqual({
      items: result.items,
      loading: false,
      error: null,
      truncated: true,
      nextCursor: "cursor-2",
      stale: false,
      lastAccessedAt: 9,
    });
    expect(loadedDirectoryEntry({ items: undefined, truncated: undefined, next_cursor: undefined }, 9)).toEqual({
      items: [], loading: false, error: null, truncated: false, nextCursor: null, stale: false, lastAccessedAt: 9,
    });
    const overridden = loadedDirectoryEntry(result, 9, []);
    expect(overridden.items).toEqual([]);
  });

  test("失败条目保留旧快照并写入错误", () => {
    expect(failedDirectoryEntry(entry(1, 3), "读取失败", 8)).toEqual({
      items: entry(1, 3).items,
      loading: false,
      error: "读取失败",
      truncated: false,
      nextCursor: null,
      stale: false,
      lastAccessedAt: 8,
    });
    expect(failedDirectoryEntry(undefined, "读取失败", 8).items).toEqual([]);
  });

  test("过期标记只翻转 stale 且返回新对象", () => {
    const original = entry(1, 2);
    const stale = markDirectoryStale(original);
    expect(stale).toEqual({ ...original, stale: true });
    expect(stale).not.toBe(original);
    expect(original.stale).toBe(false);
  });

  test("LRU 只淘汰未保护的最旧目录", () => {
    const cache = {
      root: entry(1, 2),
      old: entry(2, 2),
      recent: entry(3, 2),
    };

    const pruned = pruneDirectoryCache(cache, new Set(["root"]), 2, 10);

    expect(Object.keys(pruned).sort()).toEqual(["recent", "root"]);
  });

  test("目录恢复并发不超过配置值", async () => {
    let active = 0;
    let maximum = 0;
    await runWithConcurrency([1, 2, 3, 4, 5], 2, async () => {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setTimeout(resolve, 3));
      active -= 1;
    });

    expect(maximum).toBe(2);
  });

  test("父目录恢复失败后不再请求它的后代", async () => {
    const loaded: string[] = [];
    const parentOf = (path: string) => {
      const index = path.lastIndexOf("/");
      return index < 0 ? "" : path.slice(0, index);
    };

    await restoreDirectoriesInOrder(
      ["parent/child", "other", "parent"],
      async (path) => {
        loaded.push(path);
        return path !== "parent";
      },
      parentOf,
      2,
    );

    expect(loaded).toContain("parent");
    expect(loaded).toContain("other");
    expect(loaded).not.toContain("parent/child");
  });
});
