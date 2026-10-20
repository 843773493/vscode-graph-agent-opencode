// Integration：生产 hook/IndexedDB/HTTP 文件路由；Gateway 认证与身份入口为受控替身。
import { createServer, request as proxyRequest } from "node:http";
import { createServer as createTcpServer } from "node:net";
import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import { mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { chromium } from "playwright";

const root = process.cwd();
const mode = process.argv[2] ?? "full";
if (!["backend", "proxy", "smoke", "cleanup", "full"].includes(mode)) throw new Error(`未知测试阶段: ${mode}`);
const output = path.join(root, "out/tests/integration/clients/web/workspace_files/workspace_file_operations");
const artifacts = path.join(output, "artifacts");
const bundle = path.join(artifacts, "bundle");
const runtime = path.join(output, "runtime");
const browserTemporary = path.join(runtime, "browser-temporary");
const downloads = path.join(artifacts, "downloads");
await mkdir(bundle, { recursive: true });
const reserve = createTcpServer();
await new Promise((resolve, reject) => { reserve.once("error", reject); reserve.listen(0, "127.0.0.1", resolve); });
const backendPort = reserve.address().port;
await new Promise((resolve) => reserve.close(resolve));
const backendUrl = `http://127.0.0.1:${backendPort}`;
const modulePath = (relative) => JSON.stringify(path.join(root, "src/clients/web/src", relative));
const entry = path.join(bundle, "harness.tsx");
if (!["backend", "proxy"].includes(mode)) {
await writeFile(entry, `
import React, { useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { useWorkspaceFileOperations } from ${modulePath("components/workspace/fileTree/useWorkspaceFileOperations.ts")};
import { useWorkspaceFileTreeDirectories } from ${modulePath("components/workspace/fileTree/useWorkspaceFileTreeDirectories.ts")};
import * as storage from ${modulePath("state/workspaceFiles/intentStore.ts")};
import * as api from ${modulePath("api.ts")};
window.storage = storage;
window.api = api;
window.statuses = [];
window.errors = [];
window.committed = [];
function Harness({ route }) {
  const expanded = useRef(new Set());
  const shortcuts = useRef(new Set());
  const active = useRef(null);
  const directories = useWorkspaceFileTreeDirectories({ port: Number(location.port), workspaceId: route,
    expandedPathsRef: expanded, shortcutTreePathsRef: shortcuts, activeFilePathRef: active,
    onStatusChange: (message) => window.statuses.push(message) });
  const operations = useWorkspaceFileOperations({ port: Number(location.port), workspaceId: route,
    reconcileDirectories: directories.reconcileDirectories, acceptSnapshots: directories.acceptSnapshots,
    directoryVersion: directories.directoryVersion, onStatusChange: (message) => window.statuses.push(message),
    onError: (message) => window.errors.push(message),
    onCommitted: (parent) => { expanded.current.add(parent); window.committed.push({ route, parent }); } });
  useEffect(() => {
    let cancelled = false;
    void api.getWorkspaceFiles(Number(location.port), "", route).then((result) => {
      if (cancelled) return;
      window.fileRoot = result.directory_key.root;
      window.filePath = (relative) => window.fileRoot + (relative ? "/" + relative : "");
      expanded.current = new Set([window.filePath("target"), window.filePath("other")]);
      void directories.loadDirectory(window.filePath("target"));
      void directories.loadDirectory(window.filePath("other"));
    }).catch((error) => window.errors.push(String(error)));
    return () => { cancelled = true; };
  }, [route]);
  window.driver = { ...operations, ...directories, route };
  return <pre>{JSON.stringify({ route, directories: directories.directories, pending: operations.pendingCreations })}</pre>;
}
const renderer = createRoot(document.getElementById("root"));
window.mount = (route) => renderer.render(<Harness key={route} route={route}/>);
window.mount(new URL(location.href).searchParams.get("route") || "gw-files-a");
`, "utf8");
const built = await Bun.build({ entrypoints: [entry], target: "browser", outdir: bundle, naming: "harness.js",
  plugins: [{ name: "client-react", setup(build) {
    build.onResolve({ filter: /^react(?:-dom)?(?:\/.*)?$/ }, (args) => ({
      path: Bun.resolveSync(args.path, path.join(root, "src/clients/web")),
    }));
  } }],
});
if (!built.success) throw new AggregateError(built.logs, "文件操作生产模块 harness 打包失败");
}
const backendLogPath = path.join(artifacts, `backend-${mode}.log`);
const backendLog = createWriteStream(backendLogPath);
const backendStarted = performance.now();
// 与正式 process helper 一致：复用现有项目 .venv，生命周期 owner 直接持有 Python。
const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const backend = spawn(python, ["-u",
  "-m", "tests.integration.clients.web.workspace_files.workspace_file_operations_backend",
  "--port", String(backendPort), "--output-root", output], {
    cwd: root, env: { ...process.env, PYTHONPATH: root }, stdio: ["ignore", "pipe", "pipe"],
  });
backend.stdout.pipe(backendLog, { end: false });
backend.stderr.pipe(backendLog, { end: false });
const backendExit = new Promise((resolve, reject) => {
  backend.once("error", reject);
  backend.once("exit", (code, signal) => resolve({ code, signal }));
});

const upstreamRequests = new Set();
const proxyResponses = new Set();
const proxyErrors = [];
const sockets = new Set();
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url || "/", "http://127.0.0.1");
    if (url.pathname === "/") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end('<!doctype html><html lang="zh-CN"><title>文件操作 Integration harness</title><div id="root"></div>'
        + (mode === "proxy" ? "" : '<script type="module" src="/harness.js"></script>') + '</html>');
    } else if (url.pathname === "/harness.js") {
      response.writeHead(200, { "Content-Type": "text/javascript" });
      response.end(await readFile(path.join(bundle, "harness.js")));
    } else {
      let incoming;
      let cancelled = false;
      const fail = (error) => {
        if (cancelled) return;
        proxyErrors.push(error);
        if (response.destroyed || response.writableEnded) return;
        if (response.headersSent) response.destroy(error);
        else { response.writeHead(502); response.end(String(error)); }
      };
      const upstream = proxyRequest(new URL(request.url, backendUrl), {
        method: request.method, agent: false,
        headers: { ...request.headers, host: new URL(backendUrl).host },
      }, (received) => {
        incoming = received;
        proxyResponses.add(incoming);
        incoming.once("close", () => proxyResponses.delete(incoming));
        incoming.once("error", fail);
        if (cancelled) { incoming.destroy(); return; }
        response.writeHead(incoming.statusCode, incoming.headers);
        incoming.pipe(response);
      });
      upstreamRequests.add(upstream);
      upstream.once("close", () => upstreamRequests.delete(upstream));
      upstream.once("error", fail);
      // 显式持有原 request/IncomingMessage；客户端取消直接销毁二者。
      const disconnect = () => {
        cancelled = true;
        upstream.destroy();
        incoming?.destroy();
      };
      response.once("close", disconnect);
      request.once("aborted", disconnect);
      request.pipe(upstream);
    }
  } catch (error) {
    proxyErrors.push(error);
    if (response.destroyed || response.writableEnded) return;
    if (response.headersSent) response.destroy(error);
    else { response.writeHead(500); response.end(String(error)); }
  }
});
server.on("connection", (socket) => {
  sockets.add(socket);
  socket.once("close", () => sockets.delete(socket));
});
let browser;
let context;
let browserAlias;
let failure;
const evidence = {};
function assert(condition, message) { if (!condition) throw new Error(message); }
async function launchBrowser() {
  await mkdir(browserTemporary, { recursive: true });
  await mkdir(downloads, { recursive: true });
  // Unix socket 有真实路径长度限制；短别名仍指向正式测试 runtime。
  browserAlias = path.join(root, "out", "wf" + process.pid);
  await symlink(browserTemporary, browserAlias);
  process.env.TMPDIR = browserAlias;
  return await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true, downloadsPath: downloads, env: { ...process.env, TMPDIR: browserAlias },
  });
}
async function openContext() {
  const value = await browser.newContext();
  await value.tracing.start({ screenshots: true, snapshots: true, sources: true });
  return value;
}
async function control(url, method = "GET") {
  const response = await fetch(backendUrl + url, { method });
  assert(response.ok, `控制请求失败 ${url}: ${await response.clone().text()}`);
  return await response.json();
}
async function loadPage(page, base) {
  await page.goto(base, { waitUntil: "load" });
  await page.waitForFunction(() => window.filePath && window.driver?.directoryVersion(window.filePath("target")) && window.filePath && window.driver?.directoryVersion(window.filePath("other")));
}
function lists(inspection, route, directory) {
  return inspection.requests.filter((value) => value.route === route && value.path === "/api/v1/workspace/files"
    && (new URLSearchParams(value.query).get("path") || "") === evidence.backend.owners[route].uri + (directory ? "/" + directory : "")).length;
}
try {
  let ready = false;
  const deadline = performance.now() + 60_000;
  while (performance.now() < deadline) {
    try { const response = await fetch(backendUrl + "/control/info", { signal: AbortSignal.timeout(2_000) }); if (response.ok) { ready = true; break; } }
    catch (error) {
      if (backend.exitCode !== null) throw new Error(`真实文件 owner 启动退出 ${JSON.stringify(await backendExit)}: ${await readFile(backendLogPath, "utf8")}`, { cause: error });
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert(ready, `真实文件 HTTP owner 未在60秒内就绪: ${await readFile(backendLogPath, "utf8")}`);
  const owners = await control("/control/info");
  evidence.backend = { ready_ms: performance.now() - backendStarted, owners };
  for (const route of Object.keys(owners)) {
    const response = await fetch(backendUrl + "/api/v1/workspace/files?path=target&scope=workspace", {
      headers: { "X-BoxTeam-Workspace-Id": route },
    });
    const body = await response.json();
    assert(response.ok && body.request_id === response.headers.get("X-Request-ID")
      && body.data.directory_key.workspace_id === owners[route].workspace_id
      && body.data.directory_revision > 0, `真实文件列表入口失败: ${route} ${JSON.stringify(body)}`);
  }
  console.log(`真实文件owner就绪 ${Math.round(evidence.backend.ready_ms)}ms，两个固定工作区生产列表入口通过`);
  if (mode === "proxy") {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    browser = await launchBrowser();
    context = await openContext();
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const status = await page.evaluate(async () => {
      const controller = new AbortController();
      const response = await fetch("/api/v1/workspace/files/events", {
        method: "POST", headers: { "X-BoxTeam-Workspace-Id": "gw-files-a", "Content-Type": "application/json" },
        body: JSON.stringify({ paths: [], after_event_id: 0 }), signal: controller.signal,
      });
      // 真正消费首个 keepalive，确保 Chromium 已持有流，再取消同一 response。
      const reader = response.body.getReader();
      const first = await reader.read();
      if (first.done) throw new Error("生产 SSE 未发送首个 keepalive");
      controller.abort();
      reader.releaseLock();
      return response.status;
    });
    assert(status === 200, "代理SSE必须真实发出200头");
    await page.close();
    const deadline = performance.now() + 5_000;
    while ((upstreamRequests.size || proxyResponses.size) && performance.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 50));
    assert(upstreamRequests.size === 0 && proxyResponses.size === 0, "headers后取消必须回收原upstream");
    assert(proxyErrors.length === 0, "SSE取消不得变成代理异常");
  }
  if (!["backend", "proxy"].includes(mode)) {
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const base = `http://127.0.0.1:${server.address().port}/`;
  browser = await launchBrowser();
  context = await openContext();
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await loadPage(page, base);
  // 先各做一个真实入口样本；smoke 阶段不注入网络故障或重复完整组合。
  const samples = [
    { name: "smoke-file.txt", command: { kind: "create", name: "smoke-file.txt", entryKind: "file" } },
    { name: "smoke-directory", command: { kind: "create", name: "smoke-directory", entryKind: "directory" } },
    { name: "source-copy.txt", command: { kind: "copy", sourceScope: "workspace", sourcePath: "source-copy.txt" } },
    { name: "source-paste.txt", command: { kind: "paste", sourcePaths: [path.join(owners["gw-files-a"].root_path, "source-paste.txt")] } },
    { name: "smoke-upload.bin", command: { kind: "upload" } },
  ];
  const sampleStart = await control("/control/gw-files-a/inspect");
  for (const sample of samples) {
    const hasSource = ["copy", "paste"].includes(sample.command.kind);
    if (hasSource) await control("/control/gw-files-a/gate/pause", "POST");
    await page.evaluate(async ({ name, command }) => {
      if (command.kind === "upload") command.entries = [{ file: new Blob(["真实上传样本"]), name, relativePath: name }];
      await window.driver.submit(window.filePath("target"), command);
    }, sample);
    if (hasSource) {
      const stored = await page.evaluate(async () =>
        (await (await window.storage.openWorkspaceFileIntentStore()).loadForWorkspace("gw-files-a"))[0]);
      const sources = stored.command.kind === "copy" ? [stored.command.sourcePath] : stored.command.sourcePaths;
      assert(sources.every((value) => value.startsWith("boxteam://")), "复制来源必须在真实IndexedDB中持久化为URI");
      assert(!JSON.stringify(stored).includes(owners["gw-files-a"].root_path), "原生输入不得落入浏览器持久intent");
      await control("/control/gw-files-a/gate/release", "POST");
    }
    await page.waitForFunction((name) => window.driver.directoriesRef.current[window.filePath("target")]?.items.some((item) => item.name === name), sample.name);
  }
  const sampleEnd = await control("/control/gw-files-a/inspect");
  assert(sampleEnd.operations.length === 5 && sampleEnd.operations.every((value) => value.state === "committed"), "五类入口必须真实提交");
  assert(lists(sampleEnd, "gw-files-a", "target") === lists(sampleStart, "gw-files-a", "target"), "warm入口样本成功不能重拉父目录");
  evidence.samples = sampleEnd;
  console.log("五类生产入口单样本通过：create file/directory、copy、paste、upload，warm父目录0GET");
  if (mode === "full") {
  await control("/control/gw-files-a/gate/pause", "POST");
  let uploadAccepted = false;
  await page.route("**/api/v1/workspace/files/upload?*", async (route) => {
    const response = await route.fetch();
    assert(response.status() === 202, "真实上传路由未返回202");
    uploadAccepted = true;
    await route.abort("failed");
  });
  await page.route("**/api/v1/workspace/files/operations?*", async (route) => {
    if (uploadAccepted) await route.abort("failed"); else await route.continue();
  });
  const originalUpload = await page.evaluate(async () => {
    const bytes = Uint8Array.from({ length: 65536 }, (_, index) => index % 251);
    await window.driver.submit(window.filePath("target"), { kind: "upload", entries: [
      { file: new Blob([bytes]), name: "上传.bin", relativePath: "上传.bin" },
    ] });
    const records = await (await window.storage.openWorkspaceFileIntentStore()).loadForWorkspace("gw-files-a");
    return { id: records[0].intent.client_operation_id, sequence: records[0].intent.client_sequence,
      size: records[0].command.entries[0].file.size, errors: window.errors };
  });
  assert(uploadAccepted && originalUpload.size === 65536 && originalUpload.errors.length, "未知响应必须保存原intent和Blob并可见报错");
  await page.unroute("**/api/v1/workspace/files/upload?*");
  await page.unroute("**/api/v1/workspace/files/operations?*");
  await loadPage(page, base);
  const restored = await page.evaluate(async () => {
    const record = (await (await window.storage.openWorkspaceFileIntentStore()).loadForWorkspace("gw-files-a"))[0];
    return { id: record.intent.client_operation_id, bytes: Array.from(new Uint8Array(await record.command.entries[0].file.arrayBuffer())),
      backend: record.scope.backendWorkspaceId };
  });
  assert(restored.id === originalUpload.id && restored.backend === owners["gw-files-a"].workspace_id, "刷新不得改原身份/目标");
  assert(restored.bytes.every((byte, index) => byte === index % 251) && restored.bytes.length === 65536, "真实IDB刷新后Blob正文必须完整");
  const beforeFinish = await control("/control/gw-files-a/inspect");
  await control("/control/gw-files-a/gate/release", "POST");
  await page.waitForFunction(() => window.driver.directoriesRef.current[window.filePath("target")]?.items.some((item) => item.name === "上传.bin")
    && window.statuses.includes("文件操作已完成"));
  const afterFinish = await control("/control/gw-files-a/inspect");
  assert(afterFinish.operations.length === 6 && afterFinish.operations[5].operation_id === originalUpload.id, "响应丢失后必须查询原受理");
  assert(afterFinish.requests.filter((value) => value.path.endsWith("/upload")).length === 2, "已受理上传不得重发");
  assert(lists(afterFinish, "gw-files-a", "target") === lists(beforeFinish, "gw-files-a", "target")
    && lists(afterFinish, "gw-files-a", "other") === lists(beforeFinish, "gw-files-a", "other"), "warm成功快照不得重拉父/兄弟目录");
  const bytes = await readFile(path.join(owners["gw-files-a"].root_path, "target/上传.bin"));
  assert(bytes.length === 65536 && [...bytes].every((byte, index) => byte === index % 251), "真实文件效果必须等于原持久Blob");
  evidence.upload = { ...originalUpload, blobSha256: createHash("sha256").update(bytes).digest("hex"),
    before: beforeFinish, after: afterFinish };

  await page.route("**/api/v1/workspace/files/entries?*", (route) => route.abort("failed"));
  const unknown = await page.evaluate(async () => {
    await window.driver.submit(window.filePath("target"), { kind: "create", name: "未受理.txt", entryKind: "file" });
    return (await (await window.storage.openWorkspaceFileIntentStore()).loadForWorkspace("gw-files-a"))[0].intent;
  });
  await page.unroute("**/api/v1/workspace/files/entries?*");
  await loadPage(page, base);
  await page.waitForFunction(() => window.driver.directoriesRef.current[window.filePath("target")]?.items.some((item) => item.name === "未受理.txt"));
  const resent = await control("/control/gw-files-a/inspect");
  assert(resent.operations.length === 7 && resent.operations[6].operation_id === unknown.client_operation_id, "未受理未知结果只能沿原ID重发");
  evidence.unknownBeforeAcceptance = { original: unknown, final: resent.operations[6] };

  await control("/control/gw-files-a/gate/pause", "POST");
  const switched = await page.evaluate(async () => {
    await window.driver.submit(window.filePath("target"), { kind: "create", name: "原目标.txt", entryKind: "file" });
    return (await (await window.storage.openWorkspaceFileIntentStore()).loadForWorkspace("gw-files-a"))[0];
  });
  await page.evaluate(() => window.mount("gw-files-b"));
  await page.waitForFunction(() => window.driver.route === "gw-files-b" && window.driver.directoryVersion(window.filePath("target")));
  await control("/control/gw-files-a/gate/release", "POST");
  await page.waitForTimeout(600);
  const b = await control("/control/gw-files-b/inspect");
  assert(b.operations.length === 0 && !b.requests.some((value) => value.route === "gw-files-b"
    && ["/entries", "/copy", "/paste", "/upload"].some((suffix) => value.path.endsWith(suffix))), "切换不得把原操作改投B");
  await page.evaluate(() => window.mount("gw-files-a"));
  await page.waitForFunction(() => window.driver.route === "gw-files-a"
    && window.driver.directoriesRef.current[window.filePath("target")]?.items.some((item) => item.name === "原目标.txt"));
  await page.waitForFunction(() => window.committed.some((value) => value.route === "gw-files-a"));
  evidence.switch = { originalId: switched.intent.client_operation_id, originalScope: switched.scope,
    a: await control("/control/gw-files-a/inspect"), b };

  const beforeCold = await control("/control/gw-files-a/inspect");
  await page.evaluate(() => window.driver.submit(window.filePath("cold"), { kind: "create", name: "未展开子树", entryKind: "directory" }));
  await page.waitForFunction(() => window.driver.directoriesRef.current[window.filePath("cold")]?.items.some((item) => item.name === "未展开子树"));
  const afterCold = await control("/control/gw-files-a/inspect");
  assert(lists(afterCold, "gw-files-a", "cold") - lists(beforeCold, "gw-files-a", "cold") === 1, "cold目标只按需读取原基线");
  assert(lists(afterCold, "gw-files-a", "cold/未展开子树") === 0, "成功不能请求cold子树");
  evidence.cold = { before: beforeCold, after: afterCold };

  const batch = await page.evaluate(async () => {
    const controller = new AbortController();
    let result;
    try {
      await window.api.streamWorkspaceFileEvents(Number(location.port), [], {
        workspaceId: "gw-files-a", afterEventId: 0, signal: controller.signal,
        onBatch: (value) => { result = value; controller.abort(); },
      });
    } catch (error) { if (error.name !== "AbortError") throw error; }
    return result;
  });
  assert(batch?.event_id > 0 && batch.snapshots.length > 0 && batch.snapshots.every((snapshot) => batch.directories.some((revision) =>
    JSON.stringify(snapshot.directory_key) === JSON.stringify(revision.directory_key)
    && snapshot.directory_revision === revision.directory_revision)), "真实SSE必须保留配对父目录快照和durable cursor");
  evidence.sse = batch;

  const second = await context.newPage();
  await loadPage(second, base);
  const scope = { gatewayId: "controlled-file-browser-gateway", gatewayWorkspaceId: "idb-only",
    backendWorkspaceId: owners["gw-files-a"].workspace_id, workspaceRoot: owners["gw-files-a"].uri, actor: "local" };
  const key = { workspace_id: scope.backendWorkspaceId, scope: "workspace", root: scope.workspaceRoot, path: scope.workspaceRoot + "/target" };
  const insert = (targetPage, name) => targetPage.evaluate(async ({ scope, key, name }) =>
    (await window.storage.openWorkspaceFileIntentStore()).insert({ scope, directoryKey: key, directoryPath: key.path,
      directoryRevision: 1, command: { kind: "create", name, entryKind: "file" } }), { scope, key, name });
  const pair = await Promise.all([insert(page, "tab-a"), insert(second, "tab-b")]);
  assert(pair.map((value) => value.intent.client_sequence).sort().join(",") === "1,2", "真实多tab事务必须分配单调唯一序号");
  for (let index = 0; index < 105; index += 1) await insert(page, "frontier-" + index);
  const alias = await page.evaluate(async ({ scope, key, nativeTarget }) => {
    const store = await window.storage.openWorkspaceFileIntentStore();
    const before = await store.load(scope);
    const [targetUri] = await window.api.resolveWorkspaceFileReferences(Number(location.port), [nativeTarget], "gw-files-a");
    const aliasKey = { ...key, path: targetUri };
    const record = await store.insert({ scope, directoryKey: aliasKey, directoryPath: aliasKey.path,
      directoryRevision: 1, command: { kind: "create", name: "alias", entryKind: "file" } });
    const source = await store.insert({ scope, directoryKey: { ...key, path: scope.workspaceRoot + "/other" }, directoryPath: scope.workspaceRoot + "/other",
      directoryRevision: 1, command: { kind: "copy", sourceScope: "workspace", sourcePath: scope.workspaceRoot + "/target/tab-a" } });
    return { record, source, priorId: before.at(-1).intent.client_operation_id,
      maxDependencies: Math.max(...(await store.load(scope)).map((value) => value.intent.depends_on.length)) };
  }, { scope, key, nativeTarget: path.join(owners["gw-files-a"].root_path, "target") });
  assert(alias.record.intent.directory_revision_from_operation_id === alias.priorId, "原生输入归一同一URI目录后必须依赖原前序版本");
  assert(alias.source.intent.depends_on.includes(alias.record.intent.client_operation_id), "复制来源必须纳入前序依赖");
  assert(alias.maxDependencies <= 2, "连续意图必须只保存依赖前沿，不能突破100上限");
  evidence.multiTab = { pair: pair.map((value) => value.intent), alias };
  }
  assert(pageErrors.length === 0, "生产浏览器模块出现未处理错误: " + pageErrors.join("; "));
  }
  if (mode === "cleanup") {
    const response = await fetch(backendUrl + "/api/v1/workspace/files/events", { method: "POST",
      headers: { "X-BoxTeam-Workspace-Id": "gw-files-a", "Content-Type": "application/json" },
      body: JSON.stringify({ paths: [], after_event_id: 0 }) });
    assert(response.ok, "SSE回收样本必须进入生产文件watch入口");
    await response.body.cancel();
  }
  await writeFile(path.join(artifacts, `result-${mode}.json`), JSON.stringify({ status: "assertions_passed",
    mode, boundary: "真实Chromium/IndexedDB/Workspace文件owner；Gateway身份与认证入口为受控替身", evidence }, null, 2));
  console.log(`业务断言通过: ${mode}`);
} catch (error) {
  failure = error;
  throw error;
} finally {
  const cleanupErrors = [];
  const close = async (name, action) => {
    try { await action(); console.log(`回收完成: ${name}`); } catch (error) { cleanupErrors.push(error); }
  };
  await close("Playwright trace", async () => {
    await context?.tracing.stop({ path: path.join(artifacts, `trace-${mode}.zip`) });
  });
  await close("Chromium context", async () => { await context?.close(); });
  await close("Chromium browser", async () => { await browser?.close(); });
  await close("Chromium profile", async () => {
    if (browserAlias) await rm(browserAlias);
    await rm(browserTemporary, { recursive: true, force: true });
  });
  await close("HTTP/SSE proxy", () => new Promise((resolve) => {
    for (const upstream of upstreamRequests) upstream.destroy();
    for (const incoming of proxyResponses) incoming.destroy();
    server.close(resolve); server.closeAllConnections();
    for (const socket of sockets) socket.destroy();
  }));
  await close("Python backend", async () => {
    backend.kill("SIGTERM");
    const outcome = await Promise.race([backendExit, new Promise((resolve) => setTimeout(() => resolve(null), 10_000))]);
    const actual = outcome ?? (backend.kill("SIGKILL"), await backendExit);
    await writeFile(path.join(artifacts, `backend-exit-${mode}.json`), JSON.stringify(actual));
    backend.stdout.unpipe(backendLog);
    backend.stderr.unpipe(backendLog);
    backend.stdout.destroy();
    backend.stderr.destroy();
  });
  await close("backend log", () => new Promise((resolve) => backendLog.end(resolve)));
  cleanupErrors.push(...proxyErrors);
  if (cleanupErrors.length) throw new AggregateError(failure ? [failure, ...cleanupErrors] : cleanupErrors, "文件浏览器集成回收失败");
}
await writeFile(path.join(artifacts, `result-${mode}.json`), JSON.stringify({ status: "passed",
  mode, boundary: "真实Chromium/IndexedDB/Workspace文件owner；Gateway身份与认证入口为受控替身", evidence }, null, 2));
console.log(`PASS: ${mode}`);
