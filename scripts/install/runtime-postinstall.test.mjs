import { describe, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const fixtureRunId = randomUUID();

async function observeRuntimeDownload(scenario, scriptUrl) {
  const { default: assert } = await import("node:assert/strict");
  const fs = await import("node:fs");
  const { default: path } = await import("node:path");
  const { createHash } = await import("node:crypto");
  const { createServer } = await import("node:http");
  const { SourceTextModule, SyntheticModule } = await import("node:vm");
  const packageRoot = process.cwd();
  const archivePayload = Buffer.from("runtime下载测试正文\n".repeat(4));
  const expectedArchiveSha256 = createHash("sha256").update(archivePayload).digest("hex");
  const archiveOpenError = new Error("injected archive open failure");
  const archiveWriteError = new Error("injected archive write failure");
  const temporaryArchiveCleanupError = new Error("injected archive cleanup failure");
  const temporaryArchiveCleanupFails = ["cleanup-only", "write-and-cleanup"].includes(scenario);
  let archiveExtractionCallCount = 0;
  let archiveWriteAttemptCount = 0;
  let archiveFileCloseCount = 0;
  let httpRequestCount = 0;
  let temporaryArchiveCleanupCallCount = 0;
  let activeHttpResponse;
  let temporaryArchivePath;
  let resolveRequestSocketClosed;
  const requestSocketClosed = new Promise((resolve) => {
    resolveRequestSocketClosed = resolve;
  });
  const server = createServer((request, response) => {
    httpRequestCount += 1;
    activeHttpResponse = response;
    request.socket.once("close", resolveRequestSocketClosed);
    if (scenario === "http-rejected") {
      response.writeHead(403);
      response.write("rejection-body-still-open");
    } else if (["open-failure", "write-failure", "write-and-cleanup", "body-disconnect"].includes(scenario)) {
      response.writeHead(200, { "content-length": String(archivePayload.length * 2) });
      response.write(archivePayload);
    } else { response.end(archivePayload); }
  });
  async function within(promise, phase) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("下载fixture超时: " + phase)), 1000);
      })]);
    } finally { clearTimeout(timer); }
  }
  const isArchive = (file) => typeof file === "string" && path.basename(file).startsWith(".boxteam-runtime-");
  function recordArchiveWriteAttempt() {
    archiveWriteAttemptCount += 1;
    if (["write-failure", "write-and-cleanup"].includes(scenario)) throw archiveWriteError;
    if (scenario === "body-disconnect") activeHttpResponse.destroy();
  }
  const bindings = {
    "node:fs": {
      ...fs,
      rmSync(file, options) {
        if (isArchive(file)) {
          temporaryArchivePath = file;
          temporaryArchiveCleanupCallCount += 1;
          if (temporaryArchiveCleanupFails) throw temporaryArchiveCleanupError;
        }
        return fs.rmSync(file, options);
      },
      createWriteStream(file, options) {
        temporaryArchivePath = file;
        return fs.createWriteStream(file, {
          ...options,
          fs: {
            open(file, flags, mode, callback) {
              if (scenario === "open-failure") { queueMicrotask(() => callback(archiveOpenError)); return; }
              fs.open(file, flags, mode, callback);
            },
            close(fd, callback) { archiveFileCloseCount += 1; fs.close(fd, callback); },
            write(fd, buffer, offset, length, position, callback) {
              try { recordArchiveWriteAttempt(); } catch (error) { queueMicrotask(() => callback(error)); return; }
              fs.write(fd, buffer, offset, Math.min(length, 3), position, callback);
            },
          },
        });
      },
    },
    tar: {
      async x(options) {
        archiveExtractionCallCount += 1;
        assert.equal(options.cwd, packageRoot);
        assert.equal(options.strip, 1);
        assert.equal(options.filter("package/package.json"), false);
        assert.equal(options.filter("package/runtime-manifest.json"), true);
        assert.deepEqual(fs.readFileSync(options.file), archivePayload);
        fs.writeFileSync(path.join(packageRoot, "runtime-manifest.json"), "{}\n");
      },
    },
  };
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const url = "http://127.0.0.1:" + server.address().port + "/runtime.tgz";
    fs.writeFileSync(path.join(packageRoot, "package.json"), JSON.stringify({
      boxteam_runtime: {
        release_asset: {
          url,
          sha256: scenario === "hash-mismatch" ? "0".repeat(64) : expectedArchiveSha256,
        },
      },
    }));
    delete process.env.BOXTEAM_RUNTIME_ASSET_PATH;
    delete process.env.BOXTEAM_RUNTIME_ASSET_URL;
    if (scenario === "local-copy") {
      const source = path.join(packageRoot, "local-runtime.tgz");
      fs.writeFileSync(source, archivePayload);
      process.env.BOXTEAM_RUNTIME_ASSET_PATH = source;
    }
    // 执行原安装入口全文；只替换文件边界与tar解包，不安装依赖或复制源码。
    const module = new SourceTextModule(fs.readFileSync(new URL(scriptUrl), "utf8"), { identifier: scriptUrl });
    await module.link(async (specifier) => {
      const exports = bindings[specifier] ?? await import(specifier);
      return new SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
      });
    });
    const outcome = await within(module.evaluate().then(() => ({}), (error) => ({ error })), "安装入口终态");
    if (["remote-short-writes", "local-copy", "cleanup-only"].includes(scenario)) {
      assert.equal(
        outcome.error,
        scenario === "cleanup-only" ? temporaryArchiveCleanupError : undefined,
      );
      assert.equal(archiveExtractionCallCount, 1);
      assert.equal(fs.existsSync(path.join(packageRoot, "runtime-manifest.json")), true);
      if (scenario !== "local-copy") {
        assert(archiveWriteAttemptCount > 1, "下载必须经历真实文件边界的多次短写");
        assert.equal(archiveFileCloseCount, 1);
      }
    } else {
      assert(outcome.error instanceof Error);
      assert.equal(archiveExtractionCallCount, 0);
      assert.equal(fs.existsSync(path.join(packageRoot, "runtime-manifest.json")), false);
      if (scenario === "http-rejected") assert.match(outcome.error.message, /HTTP 403/);
      else if (scenario === "hash-mismatch") assert.match(outcome.error.message, /摘要不匹配/);
      else if (scenario === "open-failure") assert.equal(outcome.error, archiveOpenError);
      else if (scenario === "write-failure") assert.equal(outcome.error, archiveWriteError);
      else if (scenario === "write-and-cleanup") {
        assert(outcome.error instanceof AggregateError);
        assert.deepEqual(outcome.error.errors, [archiveWriteError, temporaryArchiveCleanupError]);
        assert.equal(outcome.error.cause, archiveWriteError);
      }
      else assert.match(outcome.error.message, /terminated|aborted|fetch failed/);
      if (["http-rejected", "open-failure", "write-failure", "write-and-cleanup", "body-disconnect"].includes(scenario)) {
        await within(requestSocketClosed, "失败后释放下载连接");
      }
      if (["write-failure", "write-and-cleanup", "body-disconnect", "hash-mismatch"].includes(scenario)) {
        assert.equal(archiveFileCloseCount, 1);
      }
    }
    assert.equal(httpRequestCount, scenario === "local-copy" ? 0 : 1);
    assert.equal(temporaryArchiveCleanupCallCount, 1);
    assert.equal(fs.existsSync(temporaryArchivePath), temporaryArchiveCleanupFails);
    assert.deepEqual(fs.readdirSync(packageRoot).filter((name) => name.startsWith(".boxteam-runtime-")),
      temporaryArchiveCleanupFails ? [path.basename(temporaryArchivePath)] : []);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

function runRuntimeDownload(scenario) {
  const fixtureRoot = path.resolve(
    "out/tests/scripts/install/runtime-postinstall/workspace", fixtureRunId, scenario,
  );
  const artifactRoot = path.resolve(
    "out/tests/scripts/install/runtime-postinstall/artifacts", fixtureRunId, scenario,
  );
  mkdirSync(path.dirname(fixtureRoot), { recursive: true });
  mkdirSync(fixtureRoot);
  mkdirSync(path.dirname(artifactRoot), { recursive: true });
  mkdirSync(artifactRoot);
  const input = JSON.stringify([scenario, new URL("./runtime-postinstall.mjs", import.meta.url).href]);
  const args = ["--experimental-vm-modules", "--input-type=module", "--eval", `
    ${observeRuntimeDownload.toString()}
    await observeRuntimeDownload(...JSON.parse(process.argv[1]));
  `, input];
  const run = spawnSync("node", args, { timeout: 5000, cwd: fixtureRoot });
  const stdout = run.stdout ?? Buffer.alloc(0);
  const stderr = run.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(artifactRoot, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(artifactRoot, "stderr.raw"), stderr, { flag: "wx" });
  const spawnError = run.error && {
    name: run.error.name,
    message: run.error.message,
    code: run.error.code ?? null,
    syscall: run.error.syscall ?? null,
    path: run.error.path ?? null,
  };
  writeFileSync(path.join(artifactRoot, "result.json"), `${JSON.stringify({
    scenario,
    cwd: fixtureRoot,
    command: "node",
    args: ["--experimental-vm-modules", "--input-type=module", "--eval", "<embedded observer>", input],
    status: run.status,
    signal: run.signal,
    error: spawnError ?? null,
  }, null, 2)}\n`, { flag: "wx" });
  if (run.error || run.status !== 0) {
    throw new Error(
      `Runtime下载场景 ${scenario} 失败；cwd=${fixtureRoot}；artifacts=${artifactRoot}；status=${run.status}；signal=${run.signal}；spawnError=${run.error?.message ?? "无"}；stderr:\n${stderr.toString("utf8")}`,
      { cause: run.error },
    );
  }
}

describe("Runtime安装入口的下载与文件生命周期", () => {
  for (const [scenario, description] of [
    ["remote-short-writes", "远端短写后仍通过真实摘要并交付完整归档"],
    ["local-copy", "本地归档继续走同一摘要和解包边界"],
    ["hash-mismatch", "摘要不符时拒绝解包并删除暂存归档"],
    ["http-rejected", "HTTP拒绝后立即释放未结束的响应正文"],
    ["open-failure", "无法打开归档时保留原异常并取消下载"],
    ["write-failure", "写入失败时关闭文件并取消下载"],
    ["body-disconnect", "正文断流时关闭文件且不交付残缺归档"],
    ["cleanup-only", "安装已交付但清理失败时保留原清理异常，不伪报回滚"],
    ["write-and-cleanup", "写入与清理同时失败时按序保留两原因及安装cause"],
  ]) test(description, () => runRuntimeDownload(scenario));
});
