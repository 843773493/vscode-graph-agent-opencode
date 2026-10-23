import { cp, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { describe, expect, test } from "bun:test";
import { TextFileStore } from "../src/server/fileStore.js";

const repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], {
  cwd: process.cwd(),
  encoding: "utf8",
}).trim();
const workspaceFixturePath = path.join(
  repositoryRoot,
  "tests",
  "fixtures",
  "workspaces",
  "default_test_workspace",
);
const testWorkspaceRunRoot = path.join(
  repositoryRoot,
  "out",
  "tests",
  "examples",
  "demos",
  "Ssh_text_attach",
  "tests",
  "fileStore.test",
  "workspace",
  randomUUID(),
);

async function createFileStoreWorkspace(testCaseName) {
  const workspacePath = path.join(testWorkspaceRunRoot, testCaseName);
  await mkdir(path.dirname(workspacePath), { recursive: true });
  await cp(workspaceFixturePath, workspacePath, { recursive: true });
  return workspacePath;
}

describe("TextFileStore", () => {
  test("创建、读取并保存 txt 文件", async () => {
    const workspacePath = await createFileStoreWorkspace("create-read-save");
    const store = new TextFileStore({
      filePath: path.join(workspacePath, ".boxteam", "note.txt"),
      label: "测试后端",
    });

    const initialFile = await store.snapshot();
    expect(initialFile.path.endsWith(path.join(".boxteam", "note.txt"))).toBe(true);
    expect(initialFile.content).toContain("测试后端");

    const requestedContent = "hello from test 🙂\n\uD800";
    const savedFile = await store.save(requestedContent);
    const storedContent = await Bun.file(store.filePath).text();
    expect(savedFile.content).toBe(storedContent);
    expect(savedFile.bytes).toBe(Buffer.byteLength(storedContent, "utf8"));
  });

  test("并发保存按调用顺序串行，每次响应返回本次完整快照", async () => {
    const workspacePath = await createFileStoreWorkspace("ordered-concurrent-saves");
    const store = new TextFileStore({
      filePath: path.join(workspacePath, ".boxteam", "note.txt"),
      label: "并发测试后端",
    });
    const initialFile = await store.snapshot();
    expect(initialFile.content).toContain("并发测试后端");

    const contents = [
      `first\n${"a".repeat(512 * 1024)}`,
      `second\n${"b".repeat(512 * 1024)}`,
      `third\n${"c".repeat(512 * 1024)}`,
    ];

    const firstSave = store.save(contents[0]);
    const snapshotBetweenSaves = store.snapshot();
    const laterSaves = contents.slice(1).map((content) => store.save(content));
    const [firstSavedFile, middleSnapshot, ...laterSavedFiles] = await Promise.all([
      firstSave,
      snapshotBetweenSaves,
      ...laterSaves,
    ]);
    const savedFiles = [firstSavedFile, ...laterSavedFiles];

    expect(savedFiles.map((file) => file.content)).toEqual(contents);
    expect(savedFiles.map((file) => file.bytes)).toEqual(
      contents.map((content) => Buffer.byteLength(content, "utf8")),
    );
    expect(middleSnapshot.content).toBe(contents[0]);
    expect((await store.snapshot()).content).toBe(contents.at(-1));
  });
});
