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
  const payload = Buffer.alloc(1024 * 1024 + 37, "归档正文");
  const sha256 = createHash("sha256").update(payload).digest("hex");
  const original = { ...fs };
  const originalOpen = mutableFsPromises.open;
  const openError = new Error("injected archive open failure");
  const writeError = new Error("injected archive write failure");
  const handles = [], sockets = new Set(), requestSockets = new Set();
  let activeResponse, timer;
  original.writeFileSync(path.join(fixtureRoot, "package.json"), '{"version":"fixture"}\n');
  const result = { requests: [], fullReads: 0, hashReads: 0, hashCloses: 0, maxHashChunkBytes: 0,
    bodyWrites: 0, bodyCloses: 0, retryDelays: 0 };
  if (scenario.startsWith("cache-")) {
    original.writeFileSync(archivePath, scenario === "cache-invalid" || scenario === "cache-range-rejected"
      ? payload.subarray(0, 97) : payload);
  }
  const server = createServer((request, response) => {
    requestSockets.add(request.socket);
    request.socket.once("close", () => requestSockets.delete(request.socket));
    activeResponse = response;
    result.requests.push(request.headers.range ?? null);
    if (scenario === "http-rejected") { response.writeHead(403); response.write("拒绝但保持正文未结束"); return; }
    if (scenario === "cache-range-rejected") { response.writeHead(416); response.end(); return; }
    let offset = 0;
    if (request.headers.range) {
      offset = Number(/^bytes=(\d+)-$/.exec(request.headers.range)[1]);
      response.statusCode = 206;
      response.setHeader("content-range", `bytes ${offset}-${payload.length - 1}/${payload.length}`);
    }
    if (["open-failure", "write-failure", "body-disconnect"].includes(scenario)
      || scenario === "resume-short-writes" && result.requests.length === 1) {
      response.writeHead(request.headers.range ? 206 : 200, { "content-length": payload.length - offset });
      response.write(payload.subarray(offset, offset + 8192));
      return;
    }
    response.end(scenario === "download-corrupt" ? Buffer.from("损坏归档") : payload.subarray(offset));
  });
  server.on("connection", (socket) => { sockets.add(socket); socket.once("close", () => sockets.delete(socket)); });
  function afterWrite() {
    if (scenario === "body-disconnect" || scenario === "resume-short-writes" && result.requests.length === 1) activeResponse.destroy();
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
        result.fullReads++;
        if (scenario !== "cache-reference") throw new Error("归档摘要不得整份读取正文");
      }
      return original.readFileSync(file, ...args);
    };
    mutableFs.createReadStream = (file, ...args) => {
      const stream = original.createReadStream(file, ...args);
      if (String(file) === archivePath) {
        result.hashReads++;
        stream.on("data", (chunk) => {
          result.maxHashChunkBytes = Math.max(result.maxHashChunkBytes, chunk.length);
        });
        stream.on("close", () => { result.hashCloses++; });
      }
      return stream;
    };
    mutableFs.createWriteStream = (file, options) => original.createWriteStream(file, {
      ...options,
      fs: {
        open(file, flags, mode, callback) {
          if (scenario === "open-failure") { queueMicrotask(() => callback(openError)); return; }
          original.open(file, flags, mode, callback);
        },
        close(fd, callback) { result.bodyCloses++; original.close(fd, callback); },
        write(fd, buffer, offset, length, position, callback) {
          result.bodyWrites++;
          if (scenario === "write-failure") { queueMicrotask(() => callback(writeError)); return; }
          original.write(fd, buffer, offset, scenario.includes("short-writes") ? Math.min(length, 4093) : length, position,
            (error, bytes) => { callback(error, bytes, buffer); if (!error) afterWrite(); });
        },
      },
    });
    mutableFsPromises.open = async (file, ...args) => {
      if (String(file) === archivePath && scenario === "open-failure") throw openError;
      const handle = await originalOpen(file, ...args);
      if (String(file) !== archivePath) return handle;
      handles.push(handle);
      const write = handle.write.bind(handle);
      handle.write = async (buffer, offset = 0, length = buffer.length - offset, position) => {
        result.bodyWrites++;
        if (scenario === "write-failure") throw writeError;
        const outcome = await write(buffer, offset, scenario.includes("short-writes") ? Math.min(length, 4093) : length, position);
        afterWrite();
        return outcome;
      };
      return handle;
    };
    syncBuiltinESMExports();
    const source = original.readFileSync(new URL(scriptUrl), "utf8");
    const context = createContext({
      process, Buffer, console, AbortController, AbortSignal, clearTimeout,
      // 故障场景只压缩既有一秒退避；请求期限及fixture硬期限仍使用真实时钟。
      setTimeout(callback, milliseconds, ...args) {
        if (milliseconds === 1000) { result.retryDelays++; milliseconds = 0; }
        return setTimeout(callback, milliseconds, ...args);
      },
      async fetch(url, options) {
        if (options?.headers?.Range) {
          assert.equal(Number(/^bytes=(\d+)-$/.exec(options.headers.Range)[1]), original.statSync(archivePath).size,
            "续传偏移必须等于实际落盘长度");
        }
        const response = await fetch(url, options);
        if (["body-stream", "short-writes"].includes(scenario)) {
          response.arrayBuffer = () => { throw new Error("构建下载不得缓冲完整响应正文"); };
        }
        return response;
      },
    });
    const module = new SourceTextModule(source, { identifier: scriptUrl, context });
    await module.link(async (specifier) => {
      const imported = await import(specifier.startsWith(".") ? new URL(specifier, scriptUrl).href : specifier);
      const exports = { ...imported };
      if (specifier === "./versions.mjs") {
        for (const name of ["PYTHON_RUNTIME", "PYTHON_RUNTIME_WINDOWS_X64", "NODE_RUNTIME_WINDOWS_X64"]) {
          exports[name] = { ...exports[name], archive: path.relative(fixtureRoot, archivePath), url, sha256 };
        }
      }
      return new SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
      }, { context });
    });
    await module.evaluate();
    try {
      result.archivePath = await Promise.race([
        module.namespace[{
          "linux-python": "downloadPinnedLinuxPython",
          "windows-python": "downloadPinnedWindowsPython",
          "windows-node": "downloadPinnedWindowsNode",
        }[archiveKind]](fixtureRoot),
        new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new Error("归档fixture超过1500ms")), 1500); }),
      ]);
    } catch (error) { result.error = error.message; result.originalOpenError = error === openError; result.originalWriteError = error === writeError; }
    finally { clearTimeout(timer); }
    const lifecycleFailure = ["http-rejected", "open-failure", "write-failure", "body-disconnect"].includes(scenario);
    const shouldReject = scenario === "download-corrupt" || scenario === "cache-range-rejected"
      || scenario === "cache-invalid" && archiveKind === "linux-python";
    if (lifecycleFailure) {
      if (scenario === "http-rejected") assert.match(result.error, /HTTP 403/);
      else if (scenario === "open-failure") assert.equal(result.originalOpenError, true);
      else if (scenario === "write-failure") assert.equal(result.originalWriteError, true);
      else assert.match(result.error, /terminated|aborted|fetch failed/);
      const expectedAttempts = archiveKind === "windows-python" && ["write-failure", "body-disconnect"].includes(scenario) ? 5 : 1;
      assert.equal(result.requests.length, expectedAttempts);
      assert.equal(result.retryDelays, expectedAttempts - 1);
      assert(handles.every((handle) => handle.fd === -1));
      if (archiveKind === "linux-python") assert.equal(original.existsSync(archivePath), false, "失败下载不能留下会被当作缓存的部分归档");
      if (archiveKind !== "windows-python" && ["write-failure", "body-disconnect"].includes(scenario)) assert.equal(result.bodyCloses, 1);
      await Promise.race([
        (async () => { while (requestSockets.size > 0) await new Promise((resolve) => setTimeout(resolve, 5)); })(),
        new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new Error(`下载请求连接未释放: requested=${requestSockets.size}, all=${sockets.size}`)), 500); }),
      ]);
      clearTimeout(timer);
      return result;
    }
    if (shouldReject) assert.match(result.error, /摘要不匹配/);
    else {
      assert.equal(result.error, undefined);
      assert.equal(result.archivePath, archivePath);
      assert.deepEqual(original.readFileSync(archivePath), payload);
    }
    const cacheHit = scenario === "cache-valid" || scenario === "cache-reference";
    const linuxBadCache = scenario === "cache-invalid" && archiveKind === "linux-python";
    if (scenario === "resume-short-writes") {
      assert.equal(result.requests.length, 2);
      assert.equal(result.requests[0], null);
      assert.match(result.requests[1], /^bytes=\d+-$/);
      assert.equal(result.retryDelays, 1);
    } else {
      assert.deepEqual(result.requests, cacheHit || linuxBadCache ? [] : [
        scenario.startsWith("cache-") && archiveKind === "windows-python" ? "bytes=97-" : null,
      ]);
    }
    if (scenario !== "cache-reference") {
      assert.equal(result.fullReads, 0);
      assert.equal(result.hashReads, scenario.startsWith("cache-") && !cacheHit && !linuxBadCache ? 2 : 1);
      assert.equal(result.hashCloses, result.hashReads);
      assert(result.maxHashChunkBytes > 0 && result.maxHashChunkBytes <= 64 * 1024);
    }
    if (scenario === "short-writes") assert(result.bodyWrites > 1);
    assert(handles.every((handle) => handle.fd === -1));
    return result;
  } finally {
    server.closeAllConnections();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    mutableFs.readFileSync = original.readFileSync;
    mutableFs.createReadStream = original.createReadStream;
    mutableFs.createWriteStream = original.createWriteStream;
    mutableFsPromises.open = originalOpen;
    clearTimeout(timer);
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
        const result = runPinnedArchive(archiveKind, scenario);
        expect(result.requests.length).toBeLessThanOrEqual(1);
      });
    }
    for (const scenario of ["body-stream", "short-writes", "http-rejected", "open-failure", "write-failure", "body-disconnect"]) {
      test(`${archiveKind}/${scenario}流式写盘并释放失败资源`, () => {
        expect(runPinnedArchive(archiveKind, scenario).requests.length).toBeLessThanOrEqual(5);
      });
    }
  }
  test("Windows Python的416不能绕过最终摘要校验", () => {
    expect(runPinnedArchive("windows-python", "cache-range-rejected").error).toContain("摘要不匹配");
  });
  test("Windows Python短写后断流按实际落盘长度续传，最终归档字节完整", () => {
    expect(runPinnedArchive("windows-python", "resume-short-writes").requests).toHaveLength(2);
  });
});
