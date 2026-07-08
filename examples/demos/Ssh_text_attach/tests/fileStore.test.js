import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test } from "bun:test";
import { TextFileStore } from "../src/server/fileStore.js";

describe("TextFileStore", () => {
  test("创建、读取并保存 txt 文件", async () => {
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), "ssh-text-attach-"));
    try {
      const store = new TextFileStore({
        filePath: path.join(tempRoot, ".boxteam", "note.txt"),
        label: "测试后端",
      });

      const initialFile = await store.snapshot();
      expect(initialFile.path.endsWith(path.join(".boxteam", "note.txt"))).toBe(true);
      expect(initialFile.content).toContain("测试后端");

      const savedFile = await store.save("hello from test\n");
      expect(savedFile.content).toBe("hello from test\n");
      expect(savedFile.bytes).toBe(Buffer.byteLength("hello from test\n", "utf8"));
    } finally {
      await rm(tempRoot, { recursive: true, force: true });
    }
  });
});
