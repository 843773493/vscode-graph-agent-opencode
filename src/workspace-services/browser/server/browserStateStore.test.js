import { afterAll, afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import path from "node:path";
import { cp, mkdir, readdir, rm, writeFile } from "node:fs/promises";
import * as fs from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { BrowserStateStore } from "./browserStateStore.js";

const workspaceRunRoot = path.join(process.cwd(), "out/tests/src/workspace-services/browser/server/browserStateStore/workspace", randomUUID());
// 先登记本次运行的清理，工作区复制或用例断言失败也会回收。
afterAll(() => rm(workspaceRunRoot, { recursive: true, force: true }));
afterEach(() => mock.restore());

async function createBrowserStateStoreForTest(limits) {
  const workspaceRoot = path.join(workspaceRunRoot, randomUUID());
  const workspaceId = "00000000-0000-4000-8000-000000000001";
  await cp(path.join(process.cwd(), "tests/fixtures/workspaces/browser_state_store_workspace"), workspaceRoot, { recursive: true });
  return new BrowserStateStore({ workspaceRoot, workspaceId, checkpointLimits: limits });
}

function serializedBytes(checkpoint) {
  return Buffer.byteLength(`${JSON.stringify(checkpoint)}\n`, "utf8");
}

describe("浏览器恢复记录错误归属", () => {
  test.each(["正文解析", "记录核验", "正文读取"])("%s失败保留原因且不复制原生存储位置", async (stage) => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    await mkdir(store.stateDir, { recursive: true });
    if (stage === "正文读取") await mkdir(store.stateFile);
    else await writeFile(store.stateFile, stage === "正文解析"
      ? `{"private":"${store.workspaceRoot}"`
      : JSON.stringify({ schema_version: 1, workspace_id: store.workspaceId, browsers: [] }));
    const failure = await store.readRecords().catch(error => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toBe(`读取浏览器状态失败: workspace_id=${store.workspaceId}, artifact=browser_records, stage=${stage}, code=${failure.code}`);
    expect(failure.message).not.toContain(store.workspaceRoot);
    expect(diagnostics.mock.calls[0][1].filePath).toBe(store.stateFile);
    expect(diagnostics.mock.calls[0][2]).toBe(failure.cause);
    if (stage === "正文解析") expect(failure.cause).toBeInstanceOf(SyntaxError);
    else if (stage === "正文读取") expect(failure.cause.code).toBe("EISDIR");
    else expect(failure.cause.message).toBe("浏览器状态文件格式错误");
    await rm(store.stateFile, { recursive: true });
    expect(await store.readRecords()).toBeNull();
    await store.write({ browsers: [] });
    expect(await store.readRecords()).toEqual([]);
  });

  test.each(["目录创建", "正文写入", "文件发布"])("%s失败保留错误码和原件，释放本次临时文件后可重试", async (stage) => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    if (stage === "目录创建") await writeFile(store.stateDir, "原目录位置已有文件");
    else {
      await mkdir(store.stateDir, { recursive: true });
      if (stage === "文件发布") await mkdir(store.stateFile);
      else spyOn(fs, "writeFile").mockRejectedValueOnce(Object.assign(new Error(`private=${store.stateFile}`), { code: "EIO" }));
    }
    const failure = await store.write({ browsers: [] }).catch(error => error);
    expect(failure.message).toBe(`写入浏览器状态失败: workspace_id=${store.workspaceId}, artifact=browser_records, stage=${stage}, code=${failure.code}`);
    expect(failure.message).not.toContain(store.workspaceRoot);
    expect(failure.cause.code).toBe(failure.code);
    expect(diagnostics.mock.calls[0][2]).toBe(failure.cause);
    if (stage === "目录创建") await rm(store.stateDir);
    else {
      expect(await readdir(store.stateDir)).toEqual(stage === "文件发布" ? ["browsers.json"] : []);
      if (stage === "文件发布") {
        expect(await readdir(store.stateFile)).toEqual([]);
        await rm(store.stateFile, { recursive: true });
      }
    }
    await store.write({ browsers: [] });
    expect(await store.readRecords()).toEqual([]);
  });
});

describe("浏览器截图错误归属", () => {
  test.each(["目录创建", "正文保存"])("%s失败保留原异常并允许显式重试", async (stage) => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    const original = Object.assign(new Error(`写入失败: ${store.screenshotDir}`), { code: "EIO" });
    if (stage === "目录创建") {
      await mkdir(store.stateDir, { recursive: true });
      await writeFile(store.screenshotDir, "保留原文件");
    } else {
      spyOn(fs, "writeFile").mockRejectedValueOnce(original);
    }
    const failure = await store.writeScreenshot("browser_capture", Buffer.from("png-body")).catch(error => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toContain("browser_id=browser_capture, screenshot_id=screenshot_");
    expect(failure.message).toContain(`stage=${stage}, code=${failure.code}`);
    expect(failure.message).not.toContain(store.workspaceRoot);
    expect(failure.cause).toBeInstanceOf(Error);
    expect(diagnostics.mock.calls[0][2]).toBe(failure.cause);
    expect(diagnostics.mock.calls[0][1].filePath).toContain(store.screenshotDir);
    if (stage === "正文保存") {
      expect(failure.cause).toBe(original);
      expect(failure.code).toBe("EIO");
      expect(await readdir(store.screenshotDir)).toEqual([]);
    } else {
      expect(["EEXIST", "ENOTDIR"]).toContain(failure.cause.code);
      expect(failure.code).toBe(failure.cause.code);
      await rm(store.screenshotDir);
    }
    const saved = await store.writeScreenshot("browser_capture", Buffer.from("png-body"));
    expect(saved.screenshot_id).toStartWith("screenshot_");
    expect(saved.byte_length).toBe(8);
    expect(await readdir(store.screenshotDir)).toEqual([`browser_capture-${saved.screenshot_id}.png`]);
  });
});

describe("浏览器检查点错误归属", () => {
  test("目录核验丢失条目不能冒充目标检查点不存在", async () => {
    const store = await createBrowserStateStoreForTest();
    const cause = Object.assign(new Error("目录项在列出后消失"), { code: "ENOENT" });
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    spyOn(store, "verifyCheckpointSizes").mockRejectedValueOnce(cause);
    const failure = await store.readCheckpoint("browser_missing").catch((error) => error);
    expect(failure.message).toBe("读取浏览器检查点失败: browser_id=browser_missing, stage=目录核验, code=ENOENT");
    expect(failure.cause).toBe(cause);
    expect(diagnostics.mock.calls[0][2]).toBe(cause);
    expect(await store.readCheckpoint("browser_missing")).toBeNull();
  });

  test("解析失败不复制正文或物理位置，原异常留在日志和cause", async () => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    await mkdir(store.checkpointDir, { recursive: true });
    const filePath = store.checkpointPath("browser_broken");
    await writeFile(filePath, `{"private":"${store.workspaceRoot}"`);
    const failure = await store.readCheckpoint("browser_broken").catch((error) => error);
    expect(failure.code).toBe("browser_checkpoint_read_failed");
    expect(failure.message).toBe("读取浏览器检查点失败: browser_id=browser_broken, stage=正文解析, code=browser_checkpoint_read_failed");
    expect(failure.cause).toBeInstanceOf(SyntaxError);
    expect(diagnostics.mock.calls[0][1].filePath).toBe(filePath);
    expect(diagnostics.mock.calls[0][2]).toBe(failure.cause);
    await store.deleteCheckpoint("browser_broken");
    expect(await store.readCheckpoint("browser_broken")).toBeNull();
  });

  test("发布失败保留原目录和错误码，清理临时文件后可显式重试", async () => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    const filePath = store.checkpointPath("browser_directory");
    await mkdir(filePath, { recursive: true });
    const failure = await store.writeCheckpoint("browser_directory", { body: "未发布" }).catch((error) => error);
    expect(failure.message).toBe(`写入浏览器检查点失败: browser_id=browser_directory, stage=文件发布, code=${failure.code}`);
    expect(failure.cause.code).toBe(failure.code);
    expect(failure.cause.path).toContain(store.checkpointDir);
    expect(diagnostics.mock.calls[0][2]).toBe(failure.cause);
    expect(await readdir(filePath)).toEqual([]);
    expect(await readdir(store.checkpointDir)).toEqual(["browser_directory.json"]);
    expect((await store.checkpointBudgetSnapshot()).used_bytes).toBe(0);
    await rm(filePath, { recursive: true });
    await store.writeCheckpoint("browser_directory", { body: "显式重试" });
    expect(await store.readCheckpoint("browser_directory")).toEqual({ body: "显式重试" });
  });

  test("目录核验与删除失败都不公开系统路径", async () => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => {});
    await mkdir(store.stateDir, { recursive: true });
    await writeFile(store.checkpointDir, "不能当作目录");
    const scan = await store.checkpointBudgetSnapshot().catch((error) => error);
    expect(scan.message).toBe(`核验浏览器检查点目录失败: stage=目录读取, code=${scan.code}`);
    expect(scan.cause.path).toBe(store.checkpointDir);
    expect(diagnostics.mock.calls[0][2]).toBe(scan.cause);
    await rm(store.checkpointDir);
    const filePath = store.checkpointPath("browser_directory");
    await mkdir(filePath, { recursive: true });
    const deletion = await store.deleteCheckpoint("browser_directory").catch((error) => error);
    expect(deletion.message).toBe(`删除浏览器检查点失败: browser_id=browser_directory, code=${deletion.code}`);
    expect(deletion.cause.path).toBe(filePath);
    expect(diagnostics.mock.calls.at(-1)[2]).toBe(deletion.cause);
    expect(await readdir(filePath)).toEqual([]);
  });
});

describe("浏览器检查点配额", () => {
  test("精确上限允许写入而多一个字节明确失败且保留旧检查点", async () => {
    const original = { version: 1, browser_id: "browser_quota", data: "旧状态" };
    const oversized = { ...original, data: "x".repeat(256) };
    const store = await createBrowserStateStoreForTest({
      perCheckpointMaxBytes: serializedBytes(oversized) - 1,
      workspaceCheckpointMaxBytes: 1024,
    });
    await store.writeCheckpoint("browser_quota", original);
    await expect(store.writeCheckpoint("browser_quota", oversized))
      .rejects.toMatchObject({ code: "browser_checkpoint_too_large" });
    expect(await store.readCheckpoint("browser_quota")).toEqual(original);
  });

  test("并发写入串行预留工作区配额且删除后释放配额", async () => {
    const first = { version: 1, browser_id: "browser_a", data: "a".repeat(128) };
    const second = { version: 1, browser_id: "browser_b", data: "b".repeat(128) };
    const oneCheckpointBytes = Math.max(serializedBytes(first), serializedBytes(second));
    const store = await createBrowserStateStoreForTest({
      perCheckpointMaxBytes: 1024,
      workspaceCheckpointMaxBytes: oneCheckpointBytes + 8,
    });
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
  });

  test("读取外部超大检查点前先校验字节并清理崩溃临时文件", async () => {
    const store = await createBrowserStateStoreForTest({
      perCheckpointMaxBytes: 128,
      workspaceCheckpointMaxBytes: 4096,
    });
    await mkdir(store.checkpointDir, { recursive: true });
    await writeFile(store.checkpointPath("browser_external"), JSON.stringify({ data: "x".repeat(512) }));
    await writeFile(`${store.checkpointPath("browser_external")}.orphan.tmp`, "orphan");

    await expect(store.readCheckpoint("browser_external")).rejects.toMatchObject({
      code: "browser_checkpoint_too_large",
    });
    expect((await readdir(store.checkpointDir)).some((name) => name.endsWith(".tmp"))).toBe(false);
  });

  test("配额索引建立后检测绕过软件的目录变更并明确拒绝", async () => {
    const store = await createBrowserStateStoreForTest({
      perCheckpointMaxBytes: 1024,
      workspaceCheckpointMaxBytes: 4096,
    });
    await store.writeCheckpoint("browser_indexed", { browser_id: "browser_indexed" });
    await writeFile(
      store.checkpointPath("browser_external"),
      JSON.stringify({ browser_id: "browser_external" }),
    );

    await expect(store.writeCheckpoint("browser_next", { browser_id: "browser_next" }))
      .rejects.toMatchObject({ code: "browser_checkpoint_index_changed_externally" });
  });
});

describe("浏览器产物不含真实路径", () => {
  test.each(["目录创建", "正文保存"])("下载%s失败保留原异常且不发布存储路径", async (stage) => {
    const store = await createBrowserStateStoreForTest();
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    const original = new Error(`download.saveAs: 无法写入 ${store.downloadDir}`);
    if (stage === "目录创建") {
      await mkdir(store.stateDir, { recursive: true });
      await writeFile(store.downloadDir, "保留原文件");
    }
    let saveCalls = 0;
    const download = {
      suggestedFilename: () => "report.txt",
      saveAs: async () => { saveCalls += 1; throw original; },
      url: () => "https://example.test/report.txt",
    };
    const failure = await store.writeDownload("browser_download", download).catch(error => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toContain(`browser_id=browser_download, download_id=download_`);
    expect(failure.message).toContain(`stage=${stage}, code=${failure.code}`);
    expect(failure.message).not.toContain(store.workspaceRoot);
    expect(failure.cause).toBeInstanceOf(Error);
    expect(diagnostics.mock.calls[0][2]).toBe(failure.cause);
    expect(diagnostics.mock.calls[0][1].filePath).toContain(store.downloadDir);
    expect(saveCalls).toBe(stage === "目录创建" ? 0 : 1);
    if (stage === "正文保存") {
      expect(failure.cause).toBe(original);
      expect(failure.code).toBe("browser_download_write_failed");
    } else {
      expect(["EEXIST", "ENOTDIR"]).toContain(failure.cause.code);
      expect(failure.code).toBe(failure.cause.code);
      await rm(store.downloadDir);
    }
    const recovered = await store.writeDownload("browser_download", {
      ...download, saveAs: async file => writeFile(file, "真实保存正文"),
    });
    expect(recovered.status).toBe("completed");
    expect(recovered.filename).toBe("report.txt");
  });

  test("下载与截图的持久记录只承载身份，调用栈内才重推导真实路径", async () => {
    const store = await createBrowserStateStoreForTest();
    const download = await store.writeDownload("browser_artifact", {
      suggestedFilename: () => "report.txt",
      saveAs: async () => undefined,
      url: () => "https://example.test/report.txt",
    });
    // 负向断言（6.3）：下载持久记录不得出现文件系统路径。
    expect("path" in download).toBe(false);
    expect(JSON.stringify(download).includes(store.downloadDir)).toBe(false);

    const screenshot = await store.writeScreenshot(
      "browser_artifact",
      Buffer.from("png-bytes"),
    );
    expect("path" in screenshot).toBe(false);
    expect(JSON.stringify(screenshot).includes(store.screenshotDir)).toBe(false);
  });

  test("下载越界校验由调用栈内重推导完成且强度不降低", async () => {
    const store = await createBrowserStateStoreForTest();
    // 合法身份可解析到下载目录内。
    expect(
      store.resolveDownloadPath("browser_artifact", "download_abc123", "report.txt"),
    ).toBe(path.join(store.downloadDir, "browser_artifact", "download_abc123-report.txt"));

    // 非法身份（注入路径分隔符 / 越界 ID）必须显式失败。
    expect(() => store.resolveDownloadPath("browser_artifact", "download_abc123", "../evil.txt"))
      .toThrow(/产物文件名不能包含路径分隔符/);
    expect(() => store.resolveDownloadPath("../../etc", "download_abc123", "report.txt"))
      .toThrow(/浏览器 ID 不能用于产物路径/);
    expect(() => store.resolveDownloadPath("browser_artifact", "../../etc/passwd", "report.txt"))
      .toThrow(/下载 ID 不能用于产物路径/);
  });
});
