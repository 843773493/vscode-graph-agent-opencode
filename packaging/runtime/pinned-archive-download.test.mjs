import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const outputRoot = path.resolve("out/tests/packaging/runtime/pinned-archive-download");
const runId = randomUUID();
const fixtureRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}
const observerPath = path.join(artifactsRunRoot, "observer.mjs");
writeFileSync(observerPath, `import { readFileSync } from "node:fs";
${observePinnedArchive.toString()}
console.log(JSON.stringify(await observePinnedArchive(...JSON.parse(readFileSync(process.argv[2], "utf8")))));
`, { flag: "wx" });

async function observePinnedArchive(scriptUrl, archiveKind, scenario) {
  const { default: assert } = await import("node:assert/strict");
  const fs = await import("node:fs");
  const { default: mutableFs } = await import("node:fs");
  const { default: mutableFsPromises } = await import("node:fs/promises");
  const { syncBuiltinESMExports } = await import("node:module");
  const { default: path } = await import("node:path");
  const { createHash } = await import("node:crypto");
  const { createServer } = await import("node:http");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const fixtureRoot = process.cwd();
  const archivePath = path.join(fixtureRoot, "archive.tgz");
  // 超过摘要流默认的64KiB缓冲区1字节，验证跨块读取和尾块处理。
  const archivePayload = Buffer.alloc(64 * 1024 + 1, "归档正文");
  const expectedArchiveSha256 = createHash("sha256").update(archivePayload).digest("hex");
  const originalFs = { ...fs };
  const originalFileHandleOpen = mutableFsPromises.open;
  const archiveOpenError = new Error("injected archive open failure");
  const archiveWriteError = new Error("injected archive write failure");
  const archiveFileHandles = [];
  const serverSockets = new Set();
  const activeRequestSockets = new Set();
  let activeHttpResponse;
  let fixtureDeadlineTimer;
  originalFs.writeFileSync(path.join(fixtureRoot, "package.json"), '{"version":"fixture"}\n');
  const observation = {
    rangeRequestHeaders: [],
    fullArchiveReadCount: 0,
    archiveHashReadCount: 0,
    archiveHashCloseCount: 0,
    maximumArchiveHashChunkBytes: 0,
    archiveBodyWriteCount: 0,
    archiveBodyCloseCount: 0,
    retryDelayCount: 0,
  };
  if (scenario.startsWith("cache-")) {
    originalFs.writeFileSync(archivePath, scenario === "cache-invalid" || scenario === "cache-range-rejected"
      ? archivePayload.subarray(0, 97) : archivePayload);
  }
  const server = createServer((request, response) => {
    activeRequestSockets.add(request.socket);
    request.socket.once("close", () => activeRequestSockets.delete(request.socket));
    activeHttpResponse = response;
    observation.rangeRequestHeaders.push(request.headers.range ?? null);
    if (scenario === "http-rejected") { response.writeHead(403); response.write("拒绝但保持正文未结束"); return; }
    if (scenario === "cache-range-rejected") { response.writeHead(416); response.end(); return; }
    let offset = 0;
    if (request.headers.range) {
      offset = Number(/^bytes=(\d+)-$/.exec(request.headers.range)[1]);
      response.statusCode = 206;
      response.setHeader("content-range", `bytes ${offset}-${archivePayload.length - 1}/${archivePayload.length}`);
    }
    if (["open-failure", "write-failure", "body-disconnect"].includes(scenario)
      || scenario === "resume-short-writes" && observation.rangeRequestHeaders.length === 1) {
      response.writeHead(request.headers.range ? 206 : 200, { "content-length": archivePayload.length - offset });
      response.write(archivePayload.subarray(offset, offset + 8192));
      return;
    }
    response.end(scenario === "download-corrupt" ? Buffer.from("损坏归档") : archivePayload.subarray(offset));
  });
  server.on("connection", (socket) => { serverSockets.add(socket); socket.once("close", () => serverSockets.delete(socket)); });
  function afterArchiveWrite() {
    if (scenario === "body-disconnect" || scenario === "resume-short-writes" && observation.rangeRequestHeaders.length === 1) activeHttpResponse.destroy();
  }
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const url = `http://127.0.0.1:${server.address().port}/archive.tgz`;
    process.env.BOXTEAM_PROJECT_ROOT = fixtureRoot;
    mutableFs.readFileSync = (file, ...args) => {
      if (String(file) === archivePath) {
        observation.fullArchiveReadCount++;
        if (scenario !== "cache-reference") throw new Error("归档摘要不得整份读取正文");
      }
      return originalFs.readFileSync(file, ...args);
    };
    mutableFs.createReadStream = (file, ...args) => {
      const stream = originalFs.createReadStream(file, ...args);
      if (String(file) === archivePath) {
        observation.archiveHashReadCount++;
        stream.on("data", (chunk) => {
          observation.maximumArchiveHashChunkBytes = Math.max(
            observation.maximumArchiveHashChunkBytes,
            chunk.length,
          );
        });
        stream.on("close", () => { observation.archiveHashCloseCount++; });
      }
      return stream;
    };
    mutableFs.createWriteStream = (file, options) => originalFs.createWriteStream(file, {
      ...options,
      fs: {
        open(file, flags, mode, callback) {
          if (scenario === "open-failure") { queueMicrotask(() => callback(archiveOpenError)); return; }
          originalFs.open(file, flags, mode, callback);
        },
        close(fd, callback) { observation.archiveBodyCloseCount++; originalFs.close(fd, callback); },
        write(fd, buffer, offset, length, position, callback) {
          observation.archiveBodyWriteCount++;
          if (scenario === "write-failure") { queueMicrotask(() => callback(archiveWriteError)); return; }
          originalFs.write(fd, buffer, offset, scenario.includes("short-writes") ? Math.min(length, 4093) : length, position,
            (error, bytes) => { callback(error, bytes, buffer); if (!error) afterArchiveWrite(); });
        },
      },
    });
    mutableFsPromises.open = async (file, ...args) => {
      if (String(file) === archivePath && scenario === "open-failure") throw archiveOpenError;
      const fileHandle = await originalFileHandleOpen(file, ...args);
      if (String(file) !== archivePath) return fileHandle;
      archiveFileHandles.push(fileHandle);
      const write = fileHandle.write.bind(fileHandle);
      fileHandle.write = async (buffer, offset = 0, length = buffer.length - offset, position) => {
        observation.archiveBodyWriteCount++;
        if (scenario === "write-failure") throw archiveWriteError;
        const outcome = await write(buffer, offset, scenario.includes("short-writes") ? Math.min(length, 4093) : length, position);
        afterArchiveWrite();
        return outcome;
      };
      return fileHandle;
    };
    syncBuiltinESMExports();
    const sourceModuleText = originalFs.readFileSync(new URL(scriptUrl), "utf8");
    const context = createContext({
      process, Buffer, console, AbortController, AbortSignal, clearTimeout,
      // 故障场景只压缩既有一秒退避；请求期限及fixture硬期限仍使用真实时钟。
      setTimeout(callback, milliseconds, ...args) {
        if (milliseconds === 1000) { observation.retryDelayCount++; milliseconds = 0; }
        return setTimeout(callback, milliseconds, ...args);
      },
      async fetch(url, options) {
        if (options?.headers?.Range) {
          assert.equal(Number(/^bytes=(\d+)-$/.exec(options.headers.Range)[1]), originalFs.statSync(archivePath).size,
            "续传偏移必须等于实际落盘长度");
        }
        const response = await fetch(url, options);
        if (["body-stream", "short-writes"].includes(scenario)) {
          response.arrayBuffer = () => { throw new Error("构建下载不得缓冲完整响应正文"); };
        }
        return response;
      },
    });
    const module = new SourceTextModule(sourceModuleText, { identifier: scriptUrl, context });
    await module.link(async (specifier) => {
      const imported = await import(specifier.startsWith(".") ? new URL(specifier, scriptUrl).href : specifier);
      const exports = { ...imported };
      if (specifier === "./versions.mjs") {
        for (const name of ["PYTHON_RUNTIME", "PYTHON_RUNTIME_WINDOWS_X64", "NODE_RUNTIME_WINDOWS_X64"]) {
          exports[name] = {
            ...exports[name],
            archive: path.relative(fixtureRoot, archivePath),
            url,
            sha256: expectedArchiveSha256,
          };
        }
      }
      return new SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
      }, { context });
    });
    await module.evaluate();
    try {
      observation.returnedArchivePath = await Promise.race([
        module.namespace[{
          "linux-python": "downloadPinnedLinuxPython",
          "windows-python": "downloadPinnedWindowsPython",
          "windows-node": "downloadPinnedWindowsNode",
        }[archiveKind]](fixtureRoot),
        new Promise((_resolve, reject) => {
          fixtureDeadlineTimer = setTimeout(
            () => reject(new Error("归档fixture超过1500ms")),
            1500,
          );
        }),
      ]);
    } catch (error) {
      observation.operationErrorMessage = error.message;
      observation.wasOriginalOpenError = error === archiveOpenError;
      observation.wasOriginalWriteError = error === archiveWriteError;
    } finally {
      clearTimeout(fixtureDeadlineTimer);
    }
    const isResourceLifecycleFailure = ["http-rejected", "open-failure", "write-failure", "body-disconnect"].includes(scenario);
    const shouldRejectAfterHashVerification = scenario === "download-corrupt" || scenario === "cache-range-rejected"
      || scenario === "cache-invalid" && archiveKind === "linux-python";
    if (isResourceLifecycleFailure) {
      if (scenario === "http-rejected") assert.match(observation.operationErrorMessage, /HTTP 403/);
      else if (scenario === "open-failure") assert.equal(observation.wasOriginalOpenError, true);
      else if (scenario === "write-failure") assert.equal(observation.wasOriginalWriteError, true);
      else assert.match(observation.operationErrorMessage, /terminated|aborted|fetch failed/);
      const expectedRequestCount = archiveKind === "windows-python" && ["write-failure", "body-disconnect"].includes(scenario) ? 5 : 1;
      assert.equal(observation.rangeRequestHeaders.length, expectedRequestCount);
      assert.equal(observation.retryDelayCount, expectedRequestCount - 1);
      assert(archiveFileHandles.every((fileHandle) => fileHandle.fd === -1));
      if (archiveKind === "linux-python") assert.equal(originalFs.existsSync(archivePath), false, "失败下载不能留下会被当作缓存的部分归档");
      if (archiveKind !== "windows-python" && ["write-failure", "body-disconnect"].includes(scenario)) assert.equal(observation.archiveBodyCloseCount, 1);
      await Promise.race([
        (async () => { while (activeRequestSockets.size > 0) await new Promise((resolve) => setTimeout(resolve, 5)); })(),
        new Promise((_resolve, reject) => {
          fixtureDeadlineTimer = setTimeout(
            () => reject(new Error(`下载请求连接未释放: requested=${activeRequestSockets.size}, all=${serverSockets.size}`)),
            500,
          );
        }),
      ]);
      clearTimeout(fixtureDeadlineTimer);
      return observation;
    }
    if (shouldRejectAfterHashVerification) assert.match(observation.operationErrorMessage, /摘要不匹配/);
    else {
      assert.equal(observation.operationErrorMessage, undefined);
      assert.equal(observation.returnedArchivePath, archivePath);
      assert.deepEqual(originalFs.readFileSync(archivePath), archivePayload);
    }
    const hasValidCache = scenario === "cache-valid" || scenario === "cache-reference";
    const linuxInvalidCacheRejectedWithoutRequest = scenario === "cache-invalid" && archiveKind === "linux-python";
    if (scenario === "resume-short-writes") {
      assert.equal(observation.rangeRequestHeaders.length, 2);
      assert.equal(observation.rangeRequestHeaders[0], null);
      assert.match(observation.rangeRequestHeaders[1], /^bytes=\d+-$/);
      assert.equal(observation.retryDelayCount, 1);
    } else {
      assert.deepEqual(observation.rangeRequestHeaders, hasValidCache || linuxInvalidCacheRejectedWithoutRequest ? [] : [
        scenario.startsWith("cache-") && archiveKind === "windows-python" ? "bytes=97-" : null,
      ]);
    }
    if (scenario !== "cache-reference") {
      assert.equal(observation.fullArchiveReadCount, 0);
      assert.equal(observation.archiveHashReadCount, scenario.startsWith("cache-") && !hasValidCache && !linuxInvalidCacheRejectedWithoutRequest ? 2 : 1);
      assert.equal(observation.archiveHashCloseCount, observation.archiveHashReadCount);
      assert(observation.maximumArchiveHashChunkBytes > 0 && observation.maximumArchiveHashChunkBytes <= 64 * 1024);
    }
    if (scenario === "short-writes") assert(observation.archiveBodyWriteCount > 1);
    assert(archiveFileHandles.every((fileHandle) => fileHandle.fd === -1));
    return observation;
  } finally {
    server.closeAllConnections();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    mutableFs.readFileSync = originalFs.readFileSync;
    mutableFs.createReadStream = originalFs.createReadStream;
    mutableFs.createWriteStream = originalFs.createWriteStream;
    mutableFsPromises.open = originalFileHandleOpen;
    clearTimeout(fixtureDeadlineTimer);
    syncBuiltinESMExports();
  }
}

function runPinnedArchive(archiveKind, scenario) {
  const fixtureRoot = path.join(fixtureRunRoot, archiveKind, scenario);
  const artifactsRoot = path.join(artifactsRunRoot, archiveKind, scenario);
  for (const directory of [fixtureRoot, artifactsRoot]) {
    mkdirSync(path.dirname(directory), { recursive: true });
    mkdirSync(directory);
  }
  const scriptUrl = new URL("./pinned-archive-download.mjs", import.meta.url).href;
  const inputPath = path.join(artifactsRoot, "input.json");
  writeFileSync(inputPath, `${JSON.stringify([scriptUrl, archiveKind, scenario])}\n`, { flag: "wx" });
  const args = ["--experimental-vm-modules", observerPath, inputPath];
  const run = spawnSync("node", args, { cwd: fixtureRoot, timeout: 5000 });
  const stdout = run.stdout ?? Buffer.alloc(0);
  const stderr = run.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(artifactsRoot, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(artifactsRoot, "stderr.raw"), stderr, { flag: "wx" });
  writeFileSync(path.join(artifactsRoot, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: fixtureRoot, timeout_ms: 5000,
    status: run.status, signal: run.signal,
    error: run.error ? { name: run.error.name, message: run.error.message, code: run.error.code ?? null } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (run.error || run.status !== 0) {
    throw new Error(`归档下载 ${archiveKind}/${scenario} 失败: exit=${run.status} signal=${run.signal}；artifacts=${artifactsRoot}\n${stderr.toString("utf8")}`, { cause: run.error });
  }
  return JSON.parse(stdout.toString("utf8"));
}

describe("固定版本构建归档下载", () => {
  for (const archiveKind of ["linux-python", "windows-python", "windows-node"]) {
    for (const scenario of ["cache-reference", "cache-valid", "cache-invalid", "download", "download-corrupt"]) {
      test(`${archiveKind}/${scenario}保持缓存、下载和摘要拒绝合同`, () => {
        const observation = runPinnedArchive(archiveKind, scenario);
        expect(observation.rangeRequestHeaders.length).toBeLessThanOrEqual(1);
      });
    }
    for (const scenario of ["body-stream", "short-writes", "http-rejected", "open-failure", "write-failure", "body-disconnect"]) {
      test(`${archiveKind}/${scenario}流式写盘并释放失败资源`, () => {
        expect(runPinnedArchive(archiveKind, scenario).rangeRequestHeaders.length).toBeLessThanOrEqual(5);
      });
    }
  }
  test("Windows Python的416不能绕过最终摘要校验", () => {
    expect(runPinnedArchive("windows-python", "cache-range-rejected").operationErrorMessage).toContain("摘要不匹配");
  });
  test("Windows Python短写后断流按实际落盘长度续传，最终归档字节完整", () => {
    expect(runPinnedArchive("windows-python", "resume-short-writes").rangeRequestHeaders).toHaveLength(2);
  });
});
