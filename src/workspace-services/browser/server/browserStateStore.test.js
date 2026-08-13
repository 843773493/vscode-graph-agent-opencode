import { describe, expect, test } from "bun:test";
import path from "node:path";
import { mkdir, readdir, rm, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { BrowserStateStore } from "./browserStateStore.js";

async function testStore(limits) {
  const workspaceRoot = path.join(
    process.cwd(),
    "out",
    "tests",
    "src",
    "workspace-services",
    "browser",
    "server",
    "browserStateStore",
    "workspace",
    randomUUID(),
  );
  await mkdir(workspaceRoot, { recursive: true });
  return {
    store: new BrowserStateStore({ workspaceRoot, checkpointLimits: limits }),
    cleanup: () => rm(workspaceRoot, { recursive: true, force: true }),
  };
}

function serializedBytes(checkpoint) {
  return Buffer.byteLength(`${JSON.stringify(checkpoint)}\n`, "utf8");
}

describe("浏览器检查点配额", () => {
  test("精确上限允许写入而多一个字节明确失败且保留旧检查点", async () => {
    const original = { version: 1, browser_id: "browser_quota", data: "旧状态" };
    const oversized = { ...original, data: "x".repeat(256) };
    const { store, cleanup } = await testStore({
      perCheckpointMaxBytes: serializedBytes(oversized) - 1,
      workspaceCheckpointMaxBytes: 1024,
    });
    try {
      await store.writeCheckpoint("browser_quota", original);
      await expect(store.writeCheckpoint("browser_quota", oversized))
        .rejects.toMatchObject({ code: "browser_checkpoint_too_large" });
      expect(await store.readCheckpoint("browser_quota")).toEqual(original);
    } finally {
      await cleanup();
    }
  });

  test("并发写入串行预留工作区配额且删除后释放配额", async () => {
    const first = { version: 1, browser_id: "browser_a", data: "a".repeat(128) };
    const second = { version: 1, browser_id: "browser_b", data: "b".repeat(128) };
    const oneCheckpointBytes = Math.max(serializedBytes(first), serializedBytes(second));
    const { store, cleanup } = await testStore({
      perCheckpointMaxBytes: 1024,
      workspaceCheckpointMaxBytes: oneCheckpointBytes + 8,
    });
    try {
      const results = await Promise.allSettled([
        store.writeCheckpoint("browser_a", first),
        store.writeCheckpoint("browser_b", second),
      ]);
      expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
      const rejected = results.find((result) => result.status === "rejected");
      expect(rejected.reason.code).toBe("browser_checkpoint_workspace_quota_exceeded");

      const storedId = results[0].status === "fulfilled" ? "browser_a" : "browser_b";
      const pendingId = storedId === "browser_a" ? "browser_b" : "browser_a";
      const pendingCheckpoint = pendingId === "browser_a" ? first : second;
      await store.deleteCheckpoint(storedId);
      await expect(store.writeCheckpoint(pendingId, pendingCheckpoint)).resolves.toMatchObject({
        size_bytes: serializedBytes(pendingCheckpoint),
      });
    } finally {
      await cleanup();
    }
  });

  test("读取外部超大检查点前先校验字节并清理崩溃临时文件", async () => {
    const { store, cleanup } = await testStore({
      perCheckpointMaxBytes: 128,
      workspaceCheckpointMaxBytes: 4096,
    });
    try {
      await mkdir(store.checkpointDir, { recursive: true });
      await writeFile(store.checkpointPath("browser_external"), JSON.stringify({ data: "x".repeat(512) }));
      await writeFile(`${store.checkpointPath("browser_external")}.orphan.tmp`, "orphan");

      await expect(store.readCheckpoint("browser_external")).rejects.toMatchObject({
        code: "browser_checkpoint_too_large",
      });
      expect((await readdir(store.checkpointDir)).some((name) => name.endsWith(".tmp"))).toBe(false);
    } finally {
      await cleanup();
    }
  });

  test("配额索引建立后检测绕过软件的目录变更并明确拒绝", async () => {
    const { store, cleanup } = await testStore({
      perCheckpointMaxBytes: 1024,
      workspaceCheckpointMaxBytes: 4096,
    });
    try {
      await store.writeCheckpoint("browser_indexed", { browser_id: "browser_indexed" });
      await writeFile(
        store.checkpointPath("browser_external"),
        JSON.stringify({ browser_id: "browser_external" }),
      );

      await expect(store.writeCheckpoint("browser_next", { browser_id: "browser_next" }))
        .rejects.toMatchObject({ code: "browser_checkpoint_index_changed_externally" });
    } finally {
      await cleanup();
    }
  });
});
