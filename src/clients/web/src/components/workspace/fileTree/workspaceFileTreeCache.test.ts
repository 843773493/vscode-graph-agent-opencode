import { describe, expect, test } from "bun:test";
import {
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
