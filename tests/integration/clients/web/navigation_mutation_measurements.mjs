import { createServer, request as proxyRequest } from "node:http";
import { lstat, mkdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { chromium } from "playwright";

const [configPath, artifacts, mode] = process.argv.slice(2);
if (!configPath || !artifacts) throw new Error("必须显式提供正式配置与产物目录");
if (mode && !["--observation-only", "--minimal-samples"].includes(mode)) throw new Error(`未知量化模式: ${mode}`);
const observationOnly = mode === "--observation-only";
const minimalSamples = mode === "--minimal-samples";
const focusConcurrent = process.env.BOXTEAM_NAVIGATION_FOCUS === "concurrent";
const traceConcurrent = process.env.BOXTEAM_NAVIGATION_TRACE === "concurrent";
const measurementConfig = JSON.parse(await readFile(configPath, "utf8"));
const { observationHeaders, ...browserConfig } = measurementConfig;
const bundleDirectory = path.join(artifacts, "bundle");
const repositoryRootPath = process.cwd();
const outputs = new Map();
if (!observationOnly) {
  await mkdir(bundleDirectory, { recursive: true });
  const bundle = await Bun.build({
    entrypoints: [path.join(repositoryRootPath, "tests/integration/clients/web/navigation_mutation_measurements_harness.tsx")],
    target: "browser", outdir: bundleDirectory, naming: "[name].[ext]",
    plugins: [{
      name: "frontend-react",
      setup(build) {
        build.onResolve({ filter: /^(react|react-dom)(\/.*)?$/ }, (args) => ({
          path: Bun.resolveSync(args.path, path.join(repositoryRootPath, "src/clients/web")),
        }));
      },
    }],
  });
  if (!bundle.success) throw new AggregateError(bundle.logs, "量化客户端打包失败");
  for (const output of bundle.outputs) outputs.set(`/${path.basename(output.path)}`, output.path);
}
const js = [...outputs.keys()].find((name) => name.endsWith(".js"));
const css = [...outputs.keys()].filter((name) => name.endsWith(".css"));
if (!js && !observationOnly) throw new Error("量化客户端缺少 JS bundle");
const mutationMethodByPath = new Map([
  ["/api/v1/workspace/files/entries", "POST"],
  ["/api/v1/workspace/files/paste", "POST"],
  ["/api/v1/workspace/files/copy", "POST"],
  ["/api/v1/workspace/files/upload", "POST"],
  ["/api/gateway/workspace-navigation/folders", "POST"],
  ["/api/gateway/workspace-navigation/placement", "PUT"],
]);
const isActualMutationRequest = (method, pathname) => mutationMethodByPath.get(pathname) === method;
const actualMutationRequests = [];
const rawExchangeDirectory = path.join(artifacts, "raw-exchanges");
const rawExchangeEntries = [];
let rawExchangeDirectoryReady;
let proxyExchangeSequence = 0;
let browserRequestSequence = 0;
const browserRequestIdByRequest = new WeakMap();
const expectedUploadContent = "正式量化上传内容\n";
function nodePhaseTimestamp() {
  const atMs = performance.now();
  return { at_ms: atMs, at_epoch_ms: performance.timeOrigin + atMs };
}
async function parseActualMutationPayload(url, contentType, body) {
  if (!contentType.toLowerCase().includes("multipart/form-data")) {
    return JSON.parse(body.toString("utf8"));
  }
  const form = await new Request(url, { method: "POST", headers: { "content-type": contentType }, body }).formData();
  const intent = form.get("intent");
  if (typeof intent !== "string") throw new Error("multipart 上传请求缺少 intent 字段");
  const files = form.getAll("files");
  const fileDetails = await Promise.all(files.map(async (file) => {
    if (typeof file === "string" || typeof file.name !== "string" || typeof file.text !== "function") {
      throw new Error("multipart 上传 files 字段不是文件");
    }
    const bytes = Buffer.from(await file.arrayBuffer());
    return { name: file.name, type: file.type, size: file.size,
      sha256: createHash("sha256").update(bytes).digest("hex"),
      hasExpectedContent: bytes.toString("utf8") === expectedUploadContent };
  }));
  const relativePaths = form.getAll("relative_paths");
  if (relativePaths.some((value) => typeof value !== "string")) {
    throw new Error("multipart relative_paths 字段不是文本");
  }
  return { intent: JSON.parse(intent), fileNames: fileDetails.map((file) => file.name), relativePaths,
    files: fileDetails, hasExpectedUploadContent: fileDetails.length === 1 && fileDetails[0].hasExpectedContent };
}
function actualMutationPayloadSummary(payload) {
  const summary = {};
  if (payload?.intent) summary.intent = { client_operation_id: payload.intent.client_operation_id,
    client_sequence: payload.intent.client_sequence, directory_key: payload.intent.directory_key };
  for (const key of ["name", "kind", "source_path", "source_scope", "source_paths", "node_id", "parent_node_id", "mode"]) {
    if (Object.hasOwn(payload ?? {}, key)) summary[key] = payload[key];
  }
  if (payload?.fileNames) summary.file_names = payload.fileNames;
  if (payload?.relativePaths) summary.relative_paths = payload.relativePaths;
  if (payload?.files) summary.files = payload.files.map(({ name, type, size, sha256, hasExpectedContent }) =>
    ({ name, type, size, sha256, has_expected_content: hasExpectedContent }));
  if (typeof payload?.hasExpectedUploadContent === "boolean") {
    summary.has_expected_upload_content = payload.hasExpectedUploadContent;
  }
  return summary;
}
function actualMutationRequestSummary(requestFact) {
  return { method: requestFact.method, url: requestFact.url, gateway_target_url: requestFact.gateway_target_url,
    path: requestFact.path, query: requestFact.query,
    browser_request_id: requestFact.browser_request_id, proxy_exchange_id: requestFact.proxy_exchange_id,
    proxy_state: requestFact.proxy_state, proxy_response_status: requestFact.proxy_response_status ?? null,
    proxy_error: requestFact.proxy_error ?? null, raw_body_sha256: requestFact.raw_body_sha256,
    proxy_node_phase_times: requestFact.proxy_node_phase_times,
    owner_preimage: requestFact.owner_preimage ?? null,
    owner_preimage_sha256: requestFact.owner_preimage_sha256 ?? null,
    raw_exchange_file: requestFact.raw_exchange_file, response_bytes_received: requestFact.response_bytes_received ?? 0,
    response_body_sha256: requestFact.response_body_sha256 ?? null,
    response_body_complete: requestFact.response_body_complete ?? false,
    response_aborted: requestFact.response_aborted ?? false,
    response_error: requestFact.response_error ?? null,
    request_body_complete: requestFact.request_body_complete,
    request_aborted: requestFact.request_aborted,
    request_error: requestFact.request_error ?? null,
    upstream_dispatch_started: requestFact.upstream_dispatch_started,
    auth_headers_present: requestFact.auth_headers_present,
    workspace_id_header: requestFact.workspace_id_header, has_local_token: requestFact.has_local_token,
    content_type: requestFact.content_type, body_bytes: requestFact.body_bytes,
    payload: requestFact.payload_summary, ...(requestFact.payload_error ? { payload_error: requestFact.payload_error } : {}) };
}
function operationExchangeIndex() {
  return actualMutationRequests.filter((fact) => fact.path.startsWith("/api/v1/workspace/files/"))
    .map((fact) => ({
      operation_id: fact.payload?.intent?.client_operation_id ?? null,
      client_sequence: fact.payload?.intent?.client_sequence ?? null,
      browser_request_id: fact.browser_request_id,
      proxy_exchange_id: fact.proxy_exchange_id,
      raw_exchange_file: fact.raw_exchange_file,
      proxy_node_phase_times: fact.proxy_node_phase_times,
      raw_request_body_file: `${fact.proxy_exchange_id}.request.body.bin`,
      raw_body_sha256: fact.raw_body_sha256 ?? null,
      owner_preimage_sha256: fact.owner_preimage_sha256 ?? null,
      proxy_state: fact.proxy_state,
      proxy_response_status: fact.proxy_response_status ?? null,
      payload_error: fact.payload_error ?? null,
    }));
}
const isSecretHeader = (name) => /auth|cookie|token|api[-_]key|secret/i.test(name);
function redactedRawHeaders(rawHeaders) {
  const headers = [];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    headers.push(name, isSecretHeader(name) ? "[REDACTED]" : rawHeaders[index + 1]);
  }
  return headers;
}
function registerRawExchange(fact, rawHeaders) {
  const exchange = fact.proxy_exchange_id;
  const entry = { fact, bodyPath: path.join(rawExchangeDirectory, `${exchange}.request.body.bin`),
    metadataPath: path.join(rawExchangeDirectory, `${exchange}.json`), responseChunks: [], responseFinalized: false,
    responseHeaders: [], responseStatus: null,
    metadata: { browser_request_id: fact.browser_request_id, proxy_exchange_id: exchange,
      method: fact.method, url: fact.url, gateway_target_url: fact.gateway_target_url,
      request: { raw_headers_redacted: redactedRawHeaders(rawHeaders), body_file: `${exchange}.request.body.bin`,
        body_bytes: 0, body_sha256: null, body_complete: false, aborted: false, error: null,
        upstream_dispatch_started: false, node_phase_times: fact.proxy_node_phase_times,
        auth_headers_present: fact.auth_headers_present,
        owner_preimage: null, owner_preimage_sha256: null }, response: null }, writePromise: Promise.resolve() };
  fact.raw_exchange_file = path.basename(entry.metadataPath);
  rawExchangeEntries.push(entry);
  return entry;
}
function persistRawRequest(entry, body, { complete, aborted, error }) {
  const { fact } = entry;
  fact.body_bytes = body.length;
  fact.raw_body_sha256 = createHash("sha256").update(body).digest("hex");
  fact.request_body_complete = complete;
  fact.request_aborted = aborted;
  fact.request_error = error;
  const requestMetadata = entry.metadata.request;
  requestMetadata.body_bytes = body.length;
  requestMetadata.body_sha256 = fact.raw_body_sha256;
  requestMetadata.body_complete = complete;
  requestMetadata.aborted = aborted;
  requestMetadata.error = error;
  rawExchangeDirectoryReady ??= mkdir(rawExchangeDirectory, { recursive: true });
  entry.writePromise = entry.writePromise.then(() => rawExchangeDirectoryReady)
    .then(() => writeFile(entry.bodyPath, body));
  void entry.writePromise.catch(() => undefined);
  return entry;
}
function finishRawResponse(entry, { status = null, headers = [], complete = false, aborted = false,
  error = null, origin = "upstream" } = {}) {
  if (!entry || entry.responseFinalized) return;
  entry.responseFinalized = true;
  const body = Buffer.concat(entry.responseChunks);
  const bodyHash = createHash("sha256").update(body).digest("hex");
  entry.fact.response_bytes_received = body.length;
  entry.fact.response_body_sha256 = bodyHash;
  entry.fact.response_body_complete = complete;
  entry.fact.response_aborted = aborted;
  entry.fact.response_error = error;
  entry.metadata.response = { origin, status, raw_headers_redacted: headers,
    body_file: `${entry.fact.proxy_exchange_id}.response.body.bin`,
    body_bytes_received: body.length, body_sha256: bodyHash, complete, aborted, error };
  entry.writePromise = entry.writePromise.then(async () => {
    await writeFile(path.join(rawExchangeDirectory, entry.metadata.response.body_file), body);
    await writeFile(entry.metadataPath, JSON.stringify(entry.metadata, null, 2) + "\n");
  });
  void entry.writePromise.catch(() => undefined);
}
async function flushRawExchanges() {
  if (!rawExchangeEntries.length) return;
  for (const entry of rawExchangeEntries) {
    if (!entry.responseFinalized) finishRawResponse(entry, { status: entry.responseStatus,
      headers: entry.responseHeaders, aborted: true, error: "测量结束时响应尚未完整接收" });
  }
  await Promise.all(rawExchangeEntries.map((entry) => entry.writePromise));
  const exchanges = rawExchangeEntries.map(({ fact, metadataPath, metadata }) => ({
    browser_request_id: fact.browser_request_id, proxy_exchange_id: fact.proxy_exchange_id,
    metadata_file: path.basename(metadataPath), request_body_file: metadata.request.body_file,
    response_body_file: metadata.response?.body_file ?? null,
    request_body_sha256: metadata.request.body_sha256,
    response_body_sha256: metadata.response?.body_sha256 ?? null,
  }));
  await writeFile(path.join(rawExchangeDirectory, "index.json"), JSON.stringify({
    evidence: "逐 mutation attempt 报文；正文按收到字节保存，认证头值已脱敏",
    exchange_count: exchanges.length, exchanges,
  }, null, 2) + "\n");
}
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
function ownerNormalizedIntent(intent) {
  return {
    client_operation_id: intent.client_operation_id,
    client_sequence: intent.client_sequence,
    expected_directory_revision: intent.expected_directory_revision ?? null,
    depends_on: intent.depends_on ?? [],
    created_by_operation_id: intent.created_by_operation_id ?? null,
    relative_child_path: intent.relative_child_path ?? "",
    directory_revision_from_operation_id: intent.directory_revision_from_operation_id ?? null,
    client_ref: intent.client_ref ?? null,
    directory_key: intent.directory_key,
  };
}
function fileOwnerPreimage(fact) {
  const payload = fact.payload;
  const intent = ownerNormalizedIntent(payload.intent);
  const kindByPath = { entries: "create", paste: "paste", copy: "copy", upload: "upload" };
  const endpoint = fact.path.split("/").at(-1);
  const data = endpoint === "entries" ? { name: payload.name, kind: payload.kind }
    : endpoint === "paste" ? { source_paths: payload.source_paths }
      : endpoint === "copy" ? { source_path: payload.source_path, source_scope: payload.source_scope } : {};
  const preimage = { workspace_id: intent.directory_key.workspace_id,
    directory_key: intent.directory_key, kind: kindByPath[endpoint], intent, data };
  if (endpoint === "upload") {
    preimage.uploads = payload.relativePaths.map((relativePath, index) => ({
      path: relativePath.replaceAll("\\", "/").replace(/^\/+|\/+$/g, ""),
      size: payload.files[index].size,
      hash: payload.files[index].sha256,
    }));
  }
  return preimage;
}
async function captureActualMutationRequest(request, url) {
  if (!isActualMutationRequest(request.method, url.pathname)) return { body: null, fact: null, rawEntry: null };
  const contentType = request.headers["content-type"] ?? "";
  const fact = { method: request.method, url: url.href, path: url.pathname, query: url.search,
    gateway_target_url: new URL(request.url ?? "/", measurementConfig.gatewayUrl).href,
    proxy_exchange_id: `proxy-exchange-${process.pid}-${++proxyExchangeSequence}`,
    browser_request_id: request.headers["x-test-navigation-browser-request-id"] ?? null,
    proxy_state: "request_receiving", proxy_response_status: null, proxy_error: null,
    proxy_node_phase_times: { time_origin_ms: performance.timeOrigin, request_body_observed: null,
      proxy_dispatch_started: null, upstream_response_headers: null, upstream_response_body_end: null },
    upstream_dispatch_started: false,
    workspace_id_header: request.headers["x-boxteam-workspace-id"] ?? null,
    has_local_token: Boolean(request.headers["x-local-token"]), content_type: contentType, body_bytes: 0,
    request_body_complete: false, request_aborted: false, request_error: null,
    auth_headers_present: [...new Set(request.rawHeaders.filter((_, index) => index % 2 === 0 && isSecretHeader(request.rawHeaders[index])))],
    raw_body_sha256: null, payload: null, payload_summary: null, payload_error: null };
  const rawEntry = registerRawExchange(fact, request.rawHeaders);
  const chunks = [];
  let readError = null;
  try {
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
  } catch (error) {
    readError = error;
  }
  const body = Buffer.concat(chunks);
  fact.proxy_node_phase_times.request_body_observed = nodePhaseTimestamp();
  const requestComplete = !readError && !request.aborted && request.complete;
  const requestAborted = !requestComplete;
  const readErrorMessage = readError === null ? null : readError instanceof Error ? readError.message : String(readError);
  const requestError = readErrorMessage ?? (requestAborted
    ? request.aborted ? "入站请求在正文结束前中止" : "入站请求正文未完整接收" : null);
  persistRawRequest(rawEntry, body, { complete: requestComplete, aborted: requestAborted, error: requestError });
  if (!requestComplete) {
    fact.proxy_state = "request_incomplete_not_forwarded";
    fact.proxy_error = requestError;
    actualMutationRequests.push(fact);
    rawEntry.metadata.request.upstream_dispatch_started = false;
    finishRawResponse(rawEntry, { aborted: true, error: requestError, origin: "request_not_forwarded" });
    return { body: null, fact, rawEntry, forward: false };
  }
  fact.proxy_state = "request_received";
  try {
    fact.payload = await parseActualMutationPayload(url.href, contentType, body);
    fact.payload_summary = actualMutationPayloadSummary(fact.payload);
    if (url.pathname.startsWith("/api/v1/workspace/files/")) {
      fact.owner_preimage = fileOwnerPreimage(fact);
      fact.owner_preimage_sha256 = createHash("sha256").update(canonicalJson(fact.owner_preimage)).digest("hex");
    }
  } catch (error) {
    fact.payload_error = error.message;
  }
  rawEntry.metadata.request.owner_preimage = fact.owner_preimage ?? null;
  rawEntry.metadata.request.owner_preimage_sha256 = fact.owner_preimage_sha256 ?? null;
  actualMutationRequests.push(fact);
  return { body, fact, rawEntry, forward: true };
}

const probeTraces = [];
let backendConcurrentObservation = null;
let backendConcurrentObservationError = null;
let backendConcurrentObservationAttempted = false;
let probeSequence = 0;
function traceProbe(probe, stage, details = {}) {
  if (!probe) return;
  const phase = { stage, at_ms: performance.now(), at_utc: new Date().toISOString(), ...details };
  probe.phases.push(phase);
  process.stdout.write(JSON.stringify({ navigation_fs_probe: probe.probe_id, ...phase }) + "\n");
}
async function saveBackendConcurrentObservation() {
  backendConcurrentObservationAttempted = true;
  try {
    const response = await context.request.get(new URL(
      "/api/v1/test/navigation-measurements/concurrent-trace", measurementConfig.gatewayUrl,
    ).href, { timeout: 30_000, headers: { ...observationHeaders,
      "X-BoxTeam-Workspace-Id": measurementConfig.workspaceId,
      "X-BoxTeam-Backend-Workspace-Id": measurementConfig.backendWorkspaceId } });
    let responseBody;
    try {
      responseBody = await response.text();
    } finally {
      await response.dispose();
    }
    if (!response.ok()) {
      throw new Error(`backend concurrent trace 读取失败: ${response.status()} ${responseBody}`);
    }
    const snapshot = JSON.parse(responseBody);
    backendConcurrentObservation = snapshot;
    await writeFile(path.join(artifacts, "backend-concurrent-observation.json"),
      JSON.stringify(snapshot, null, 2) + "\n");
    return snapshot;
  } catch (error) {
    backendConcurrentObservationError = error.message;
    throw error;
  }
}
function validateBackendConcurrentObservation(snapshot) {
  if (!Array.isArray(snapshot.events) || !Number.isSafeInteger(snapshot.dropped_events)
    || snapshot.dropped_events !== 0) {
    const error = new Error(`backend concurrent trace 非法或超出有界缓冲: ${JSON.stringify({
      count: snapshot.events?.length,
      dropped_events: snapshot.dropped_events,
    })}`);
    backendConcurrentObservationError = error.message;
    throw error;
  }
}
function forwardedHeaders(headers) {
  const forwarded = { ...headers };
  // 每跳连接由各自 owner 管理；客户端的 keep-alive 不得控制代理上游。
  const connectionNames = String(headers.connection ?? "").split(",").map((name) => name.trim().toLowerCase());
  for (const name of [...connectionNames, "connection", "keep-alive", "proxy-connection", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"]) delete forwarded[name];
  return forwarded;
}
const proxyResponses = new Set();
const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "127.0.0.1"}`);
  if (url.pathname.startsWith("/api/")) {
    const { body, fact, rawEntry } = await captureActualMutationRequest(request, url);
    if (fact && !fact.request_body_complete) {
      response.destroy();
      return;
    }
    const upstream = proxyRequest(new URL(request.url, measurementConfig.gatewayUrl), {
      method: request.method, agent: false,
      headers: { ...forwardedHeaders(request.headers), host: new URL(measurementConfig.gatewayUrl).host, connection: "close" },
    }, (incoming) => {
      if (fact) fact.proxy_node_phase_times.upstream_response_headers = nodePhaseTimestamp();
      proxyResponses.add(incoming);
      incoming.once("close", () => proxyResponses.delete(incoming));
      const rawResponseHeaders = rawEntry ? redactedRawHeaders(incoming.rawHeaders) : [];
      if (rawEntry) {
        rawEntry.responseHeaders = rawResponseHeaders;
        rawEntry.responseStatus = incoming.statusCode;
      }
      if (rawEntry) incoming.on("data", (chunk) => rawEntry.responseChunks.push(Buffer.from(chunk)));
      incoming.once("end", () => {
        if (fact) fact.proxy_node_phase_times.upstream_response_body_end = nodePhaseTimestamp();
        finishRawResponse(rawEntry, { status: incoming.statusCode, headers: rawResponseHeaders, complete: true });
      });
      incoming.once("aborted", () => finishRawResponse(rawEntry, { status: incoming.statusCode,
        headers: rawResponseHeaders, aborted: true, error: "上游响应正文在结束前中止" }));
      incoming.once("error", (error) => finishRawResponse(rawEntry, { status: incoming.statusCode,
        headers: rawResponseHeaders, aborted: true, error: error.message }));
      incoming.once("close", () => {
        if (!rawEntry?.responseFinalized) finishRawResponse(rawEntry, { status: incoming.statusCode,
          headers: rawResponseHeaders, complete: Boolean(incoming.complete), aborted: !incoming.complete,
          error: incoming.complete ? null : "上游响应连接在正文结束前关闭" });
      });
      response.once("close", () => {
        if (!response.writableFinished && fact) {
          fact.proxy_state = "downstream_aborted";
          fact.proxy_error ??= "浏览器在代理响应完成前关闭连接";
        }
        incoming.destroy();
      });
      const headers = forwardedHeaders(incoming.headers);
      if (fact) {
        fact.proxy_state = "response_started";
        fact.proxy_response_status = incoming.statusCode;
        headers["x-test-navigation-proxy-exchange-id"] = fact.proxy_exchange_id;
        if (fact.browser_request_id) {
          headers["x-test-navigation-browser-request-id"] = fact.browser_request_id;
        }
      }
      response.writeHead(incoming.statusCode, headers);
      response.once("finish", () => { if (fact) fact.proxy_state = "response_completed"; });
      incoming.once("aborted", () => {
        if (fact) { fact.proxy_state = "response_aborted"; fact.proxy_error = "上游响应在结束前中止"; }
      });
      incoming.once("error", (error) => {
        if (fact) { fact.proxy_state = "response_error"; fact.proxy_error = error.message; }
      });
      incoming.pipe(response);
    });
    upstream.on("error", (error) => {
      if (fact) { fact.proxy_state = "upstream_error"; fact.proxy_error = error.message; }
      if (rawEntry) {
        rawEntry.responseChunks.push(Buffer.from(error.message));
        finishRawResponse(rawEntry, { status: 502, complete: true, error: error.message, origin: "proxy_error" });
      }
      if (!response.headersSent) response.writeHead(502);
      response.end(error.message);
    });
    response.on("close", () => upstream.destroy());
    if (fact) {
      fact.upstream_dispatch_started = true;
      fact.proxy_node_phase_times.proxy_dispatch_started = nodePhaseTimestamp();
      rawEntry.metadata.request.upstream_dispatch_started = true;
    }
    if (body === null) request.pipe(upstream);
    else upstream.end(body);
    return;
  }
  if (outputs.has(url.pathname)) {
    response.writeHead(200, { "Content-Type": url.pathname.endsWith(".css") ? "text/css" : "text/javascript" });
    response.end(await readFile(outputs.get(url.pathname)));
    return;
  }
  if (url.pathname !== "/") { response.writeHead(404); response.end(url.pathname); return; }
  const expandedCount = Number(url.searchParams.get("expanded") ?? 1);
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  response.end(`<!doctype html><html><head>${css.map((name) => `<link rel="stylesheet" href="${name}">`).join("")}</head><body><div id="root"></div><script>window.measurementConfig=${JSON.stringify({ ...browserConfig, expandedCount })};</script>${js ? `<script type="module" src="${js}"></script>` : ""}</body></html>`);
});
await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
const address = server.address();
if (!address || typeof address === "string") throw new Error("量化客户端未取得端口");
const baseUrl = `http://127.0.0.1:${address.port}`;
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined, headless: true });
const context = await browser.newContext({ viewport: { width: 1300, height: 10000 }, permissions: ["clipboard-read", "clipboard-write"] });
await context.addCookies(measurementConfig.cookies.map((cookie) => ({ ...cookie, url: baseUrl })));
const browserApiRequestRecords = [];
const mutationSamples = [];
const clockAlignmentSamples = [];
const clockAlignmentErrors = [];
let crossPageObservation = null;
const gatewaySamples = [];
const failures = [];
let concurrentActions = [];
let concurrentExpectedNames = [];
let concurrentMissingFromUi = [];
const observationRequests = [];
const uiStatusObservations = [];
const fileUiFailureDiagnostics = [];
const responseTasks = new Set();
const pageRequests = new WeakMap();
const pageIds = new WeakMap();
let pageNumber = 0;
const sameDirectoryKey = (actual, expected) => actual?.workspace_id === expected.workspace_id
  && actual.scope === expected.scope && actual.root === expected.root && actual.path === expected.path;
function attachMeasurementPageObservers(page) {
  const pageId = ++pageNumber;
  const ownedRequests = [];
  pageRequests.set(page, ownedRequests);
  pageIds.set(page, pageId);
  const tracked = new Map();
  page.on("pageerror", (error) => failures.push(error.message));
  for (const endpoint of mutationMethodByPath.keys()) {
    page.route(`**${endpoint}**`, async (route) => {
      const request = route.request();
      if (!isActualMutationRequest(request.method(), new URL(request.url()).pathname)) {
        await route.continue();
        return;
      }
      const headers = await request.allHeaders();
      const browserRequestId = `browser-request-${process.pid}-${++browserRequestSequence}`;
      browserRequestIdByRequest.set(request, browserRequestId);
      headers["x-test-navigation-browser-request-id"] = browserRequestId;
      await route.continue({ headers });
    });
  }
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) return;
    const requestObserved = nodePhaseTimestamp();
    const record = { page: pageId, start: requestObserved.at_ms, method: request.method(), path: url.pathname,
      query: url.search, status: null, browser_state: "request_started", network_state: "request_started",
      response_observation_state: "pending",
      node_phase_times: { time_origin_ms: performance.timeOrigin, request_event: requestObserved,
        response_event: null, response_headers_observed: null, response_body_observed_complete: null,
        response_observation_complete: null, requestfinished_event: null, requestfailed_event: null },
      browser_request_id: browserRequestIdByRequest.get(request)
        ?? request.headers()["x-test-navigation-browser-request-id"] ?? null };
    let finishNetwork;
    Object.defineProperty(record, "network_finished", { value: new Promise((resolve) => { finishNetwork = resolve; }) });
    Object.defineProperty(record, "finish_network", { value: finishNetwork });
    if (isActualMutationRequest(request.method(), url.pathname)) {
      Object.defineProperty(record, "request", { value: request });
    }
    tracked.set(request, record);
    ownedRequests.push(record);
    browserApiRequestRecords.push(record);
  });
  page.on("response", (response) => {
    const record = tracked.get(response.request());
    if (!record) return;
    record.node_phase_times.response_event = nodePhaseTimestamp();
    record.status = response.status();
    if (record.network_state === "request_started") record.network_state = "response_received";
    if (record.network_state !== "request_failed") record.browser_state = "response_received";
    record.response_observation_state = "observing";
    const task = (async () => {
      const headers = await response.allHeaders();
      record.node_phase_times.response_headers_observed = nodePhaseTimestamp();
      record.backend_reads = headers["x-test-backend-fs-reads"] ? JSON.parse(headers["x-test-backend-fs-reads"]) : null;
      record.gateway_reads = headers["x-test-gateway-fs-reads"] ? JSON.parse(headers["x-test-gateway-fs-reads"]) : null;
      record.request_id = headers["x-request-id"] ?? null;
      record.proxy_exchange_id = headers["x-test-navigation-proxy-exchange-id"] ?? null;
      record.browser_request_id ??= headers["x-test-navigation-browser-request-id"] ?? null;
      if (isActualMutationRequest(record.method, record.path)
        && record.path.startsWith("/api/v1/workspace/files/") && record.status === 202) {
        record.receipt = (await response.json()).data;
        record.node_phase_times.response_body_observed_complete = nodePhaseTimestamp();
      } else if (record.path === "/api/v1/workspace/files/operations" && record.status === 200) {
        record.operation_status = (await response.json()).data;
        record.node_phase_times.response_body_observed_complete = nodePhaseTimestamp();
      } else if (minimalSamples && isActualMutationRequest(record.method, record.path)
        && record.path.startsWith("/api/gateway/workspace-navigation/") && record.status === 200) {
        const navigation = (await response.json()).data;
        record.node_phase_times.response_body_observed_complete = nodePhaseTimestamp();
        record.navigation_nodes = navigation.nodes.map(({ node_id, name, kind, parent_node_id, workspace_id }) =>
          ({ node_id, name, kind, parent_node_id, workspace_id }));
      }
    })().then(async () => {
      if (isActualMutationRequest(record.method, record.path)
        || record.path === "/api/v1/workspace/files/operations") await record.network_finished;
      const observed = nodePhaseTimestamp();
      record.observed_at = observed.at_ms;
      record.node_phase_times.response_observation_complete = observed;
      record.response_observation_state = "completed";
      if (record.network_state === "response_completed") record.browser_state = "response_completed";
    }, async (error) => {
      record.response_error = error.message;
      record.node_phase_times.response_body_observation_failed = nodePhaseTimestamp();
      record.response_observation_state = "failed";
      if (isActualMutationRequest(record.method, record.path)
        || record.path === "/api/v1/workspace/files/operations") await record.network_finished;
      if (record.network_state !== "request_failed") record.browser_state = "response_observation_failed";
    });
    responseTasks.add(task);
    void task.then(() => responseTasks.delete(task));
  });
  page.on("requestfinished", (request) => {
    const record = tracked.get(request);
    if (!record || record.network_state === "request_failed") return;
    record.node_phase_times.requestfinished_event = nodePhaseTimestamp();
    record.network_state = "response_completed";
    // 网络结束不等于观察结束；响应头和回执完整解析后才允许配对 202。
    if (record.response_observation_state === "completed") record.browser_state = "response_completed";
    record.finish_network();
  });
  page.on("requestfailed", (request) => {
    const record = tracked.get(request);
    if (!record) return;
    record.node_phase_times.requestfailed_event = nodePhaseTimestamp();
    record.network_state = "request_failed";
    record.browser_state = "request_failed";
    record.browser_error = request.failure()?.errorText ?? "浏览器请求失败";
    record.finish_network();
  });
  return pageId;
}
async function settleResponseTasks() {
  // 当前批次等待期间仍可能收到新响应，必须一直收口到没有未完成的观察任务。
  while (responseTasks.size) await Promise.all([...responseTasks]);
}
async function observeUiStatus(page) {
  const observation = await page.evaluate(() => ({ events: window.navigationHarness?.fileStatusEvents ?? [],
    pending_terminals: window.navigationHarness?.pendingTerminals ?? [],
    message: document.getElementById("file-status")?.textContent ?? null,
    completed_count: Number(document.getElementById("file-status")?.dataset.completedCount ?? 0) }))
    .then((value) => ({ page: pageIds.get(page), ...value }),
      (error) => ({ page: pageIds.get(page), observation_error: error.message }));
  uiStatusObservations.push(observation);
}
const waitForMeasurementPageDelay = (page, ms) => page.waitForTimeout(ms);
function nodeClockSample() {
  const timeOriginMs = performance.timeOrigin;
  const nowMs = performance.now();
  return { time_origin_ms: timeOriginMs, now_ms: nowMs, epoch_ms: timeOriginMs + nowMs };
}
async function captureClockAlignment(page, label) {
  const nodeBefore = nodeClockSample();
  const browser = await page.evaluate(() => {
    const timeOriginMs = performance.timeOrigin;
    const nowMs = performance.now();
    return { time_origin_ms: timeOriginMs, now_ms: nowMs, epoch_ms: timeOriginMs + nowMs };
  });
  const nodeAfter = nodeClockSample();
  try {
    const values = [nodeBefore.time_origin_ms, nodeBefore.now_ms, nodeBefore.epoch_ms,
      browser.time_origin_ms, browser.now_ms, browser.epoch_ms,
      nodeAfter.time_origin_ms, nodeAfter.now_ms, nodeAfter.epoch_ms];
    if (!values.every(Number.isFinite) || nodeAfter.epoch_ms < nodeBefore.epoch_ms
      || browser.epoch_ms !== browser.time_origin_ms + browser.now_ms) {
      throw new Error(`Node/Bun与Chromium时钟采样字段非法: ${JSON.stringify({ label, nodeBefore, browser, nodeAfter })}`);
    }
    const sample = { label, node_before: nodeBefore, browser, node_after: nodeAfter,
      node_call_span_ms: nodeAfter.epoch_ms - nodeBefore.epoch_ms,
      browser_offset_from_node_before_ms: browser.epoch_ms - nodeBefore.epoch_ms,
      node_after_minus_browser_ms: nodeAfter.epoch_ms - browser.epoch_ms };
    clockAlignmentSamples.push(sample);
    return sample;
  } catch (error) {
    clockAlignmentErrors.push({ label, error: error instanceof Error ? error.message : String(error) });
    throw error;
  }
}
const fileTree = (page) => page.getByRole("tree", { name: "工作区文件树" });
const fileTreeTitle = (treePath) => path.resolve(measurementConfig.workspaceRoot, treePath);
const waitForTreePaint = (page) => page.evaluate(() => new Promise((resolve) =>
  requestAnimationFrame(() => requestAnimationFrame(resolve))));
async function treeNavigationFacts(page) {
  return fileTree(page).evaluate((tree) => {
    const scroller = tree.querySelector("[data-virtuoso-scroller]") ?? tree;
    return { aria_busy: tree.getAttribute("aria-busy"), scroll_top: scroller.scrollTop,
      client_height: scroller.clientHeight, scroll_height: scroller.scrollHeight,
      virtualized: Boolean(tree.querySelector(".files-tree-virtualized")),
      rows: [...tree.querySelectorAll("[title]")].slice(0, 40).map((row) => ({
        title: row.getAttribute("title"), expanded: row.getAttribute("aria-expanded"),
        chevron: row.querySelector(".files-tree-chevron")?.className ?? null,
      })) };
  });
}
async function treeRowViewport(row, reveal = false) {
  return row.evaluateAll((elements, shouldReveal) => {
    const element = elements[0];
    if (!element?.isConnected) return null;
    const tree = element.closest('[role="tree"]');
    if (!tree) throw new Error(`已连接的文件树行缺少 tree 祖先: ${element.getAttribute("title")}`);
    const scroller = element.closest("[data-virtuoso-scroller]") ?? tree;
    const bounds = element.getBoundingClientRect();
    const viewport = scroller.getBoundingClientRect();
    const delta = bounds.top < viewport.top ? bounds.top - viewport.top
      : bounds.bottom > viewport.bottom ? bounds.bottom - viewport.bottom : 0;
    if (shouldReveal && delta) scroller.scrollTop += delta;
    return bounds.width > 0 && bounds.height > 0
      && bounds.top >= viewport.top && bounds.bottom <= viewport.bottom;
  }, reveal);
}
async function findVisibleTreeRow(page, treePath) {
  const tree = fileTree(page);
  const row = tree.getByTitle(fileTreeTitle(treePath), { exact: true });
  const reveal = async () => {
    if (await treeRowViewport(row, true) === true) return true;
    await waitForTreePaint(page);
    return await treeRowViewport(row) === true;
  };
  if (await reveal()) return row;
  const scroller = tree.locator("[data-virtuoso-scroller]").first();
  const surface = await tree.locator(".files-tree-virtualized").count() ? scroller : tree;
  await surface.evaluate((element) => { element.scrollTop = 0; });
  await waitForTreePaint(page);
  // 虚拟行离开 DOM 后，必须滚动真实视口逐段寻找，不能只等待 locator 挂载。
  for (let step = 0; step < 512; step += 1) {
    if (await reveal()) return row;
    const position = await surface.evaluate((element) => ({
      top: element.scrollTop, maximum: Math.max(0, element.scrollHeight - element.clientHeight),
      step: Math.max(120, Math.floor(element.clientHeight * 0.7)),
    }));
    if (position.top >= position.maximum) return null;
    await surface.evaluate((element, amount) => {
      element.scrollTop = Math.min(element.scrollTop + amount, element.scrollHeight - element.clientHeight);
    }, position.step);
    await waitForTreePaint(page);
  }
  throw new Error(`文件树滚动超过有界步数 ${fileTreeTitle(treePath)}: ${JSON.stringify(await treeNavigationFacts(page))}`);
}
async function treeDirectoryExpanded(row) {
  return row.evaluate((element) => {
    const expanded = element.getAttribute("aria-expanded");
    if (expanded !== null) return expanded === "true";
    const chevron = element.querySelector(".files-tree-chevron");
    if (chevron?.classList.contains("codicon-chevron-down")) return true;
    if (chevron?.classList.contains("codicon-chevron-right")) return false;
    throw new Error(`目录行没有展开状态: ${element.getAttribute("title")}`);
  });
}
async function ensureTreeDirectoryExpanded(page, treePath) {
  const row = await visibleTreePath(page, treePath);
  if (!await treeDirectoryExpanded(row)) await row.click();
  await page.waitForFunction(() => document.querySelector('[aria-label="工作区文件树"]')?.getAttribute("aria-busy") === "false");
  await waitForTreePaint(page);
}
async function visibleTreePath(page, treePath) {
  const existing = await findVisibleTreeRow(page, treePath);
  if (existing) return existing;
  const parts = treePath.split("/").filter(Boolean);
  if (parts.length) {
    await ensureTreeDirectoryExpanded(page, "");
    for (let index = 1; index < parts.length; index += 1) {
      await ensureTreeDirectoryExpanded(page, parts.slice(0, index).join("/"));
    }
  }
  // listing 与 paint 可以晚于 aria-busy 的瞬间 false；保留原 locator 的 30 秒等待预算。
  const deadline = performance.now() + 30000;
  while (performance.now() < deadline) {
    const row = await findVisibleTreeRow(page, treePath);
    if (row) return row;
    await waitForMeasurementPageDelay(page, 25);
  }
  throw new Error(`文件树中未找到可见导航行 ${fileTreeTitle(treePath)}: ${JSON.stringify(await treeNavigationFacts(page))}`);
}
async function openReadyMeasurementPage(page, expanded) {
  await page.goto(`${baseUrl}/?expanded=${expanded}`, { waitUntil: "domcontentloaded" });
  await fileTree(page).waitFor();
  const layout = await fileTree(page).evaluate((element) => ({
    parentDisplay: getComputedStyle(element.parentElement).display,
    height: element.getBoundingClientRect().height,
  }));
  if (layout.parentDisplay !== "flex" || layout.height < 6000) throw new Error(`生产文件树样式未装配: ${JSON.stringify(layout)}`);
  await page.waitForFunction(() => document.querySelector('[aria-label="工作区文件树"]')?.getAttribute("aria-busy") === "false");
  await fileTree(page).getByTitle(fileTreeTitle("d000"), { exact: true }).waitFor();
  const deadline = performance.now() + 60000;
  const expected = Array.from({ length: expanded }, (_, index) => `d${String(index).padStart(3, "0")}`);
  while (!expected.every((directory) => pageRequests.get(page).some((record) =>
    record.path === "/api/v1/workspace/files" && new URLSearchParams(record.query).get("path") === directory
    && record.status === 200))) {
    if (performance.now() >= deadline) throw new Error(`未真实加载 ${expanded} 个展开目录`);
    await waitForMeasurementPageDelay(page, 50);
  }
  await page.waitForFunction(() => Boolean(window.gatewayMutation?.nodes().length
    && window.navigationHarness?.ready && window.navigationHarness?.observerReady));
  await waitForMeasurementPageDelay(page, 650);
}
async function contextMenu(page, treePath) {
  const row = await visibleTreePath(page, treePath);
  await row.click({ button: "right" });
  await page.getByRole("menu").waitFor();
}
async function positionTreeRowAtTop(page, treePath) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const row = await visibleTreePath(page, treePath);
    const positioned = await row.evaluateAll((elements) => {
      const element = elements[0];
      if (!element?.isConnected) return false;
      const tree = element.closest('[role="tree"]');
      if (!tree) throw new Error(`已连接的文件树行缺少 tree 祖先: ${element.getAttribute("title")}`);
      const scroller = element.closest("[data-virtuoso-scroller]") ?? tree;
      scroller.scrollTop += element.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 48;
      return true;
    });
    if (positioned) return row;
    await waitForTreePaint(page);
  }
  throw new Error(`文件树行在 8 次 DOM 重取后仍未连接: ${fileTreeTitle(treePath)}`);
}
async function waitForActualVisibleTreeRow(page, treePath, deadline) {
  const row = fileTree(page).getByTitle(fileTreeTitle(treePath), { exact: true });
  while (performance.now() < deadline) {
    if (await row.count() && await treeRowViewport(row) === true) return true;
    await waitForMeasurementPageDelay(page, 20);
  }
  throw new Error(`发起页未实际呈现并显示目标路径: ${fileTreeTitle(treePath)}`);
}
let menuFailureSequence = 0;
async function prepareFileMutationMenu(page, directory) {
  await ensureTreeDirectoryExpanded(page, directory);
  const row = await positionTreeRowAtTop(page, directory);
  // 在统计窗口前把目标目录放到视口顶部，发布后的 locator 等待不再滚动或展开祖先。
  await waitForTreePaint(page);
  const actionTraceKey = "__boxteamNavigationMenuActionTrace";
  await page.evaluate((key) => {
    const trace = { events: [], listeners: [] };
    for (const type of ["pointerdown", "mousedown", "contextmenu", "mouseup", "pointerup", "click"]) {
      const listener = (event) => {
        if (trace.events.length >= 12) return;
        const target = event.target instanceof Element ? event.target : null;
        trace.events.push({ type: event.type, at_ms: performance.now(), button: event.button,
          buttons: event.buttons, client_x: event.clientX, client_y: event.clientY,
          default_prevented: event.defaultPrevented,
          target: target ? { tag: target.tagName.toLowerCase(), role: target.getAttribute("role"),
            title: target.getAttribute("title"), class_name: typeof target.className === "string"
              ? target.className.slice(0, 120) : null,
            text: (target.textContent ?? "").trim().slice(0, 100) } : null });
      };
      document.addEventListener(type, listener, true);
      trace.listeners.push([type, listener]);
    }
    window[key] = trace;
  }, actionTraceKey);
  const takeActionTrace = () => page.evaluate((key) => {
    const trace = window[key];
    if (!trace) return [];
    for (const [type, listener] of trace.listeners) document.removeEventListener(type, listener, true);
    delete window[key];
    return trace.events;
  }, actionTraceKey);
  const captureFailure = async (phase, error) => {
    let diagnosticDeadlineTimer;
    try {
      const diagnosticDeadline = new Promise((_, reject) => {
        diagnosticDeadlineTimer = setTimeout(() => reject(new Error("菜单失败边界诊断超过 3000ms 总期限")), 3000);
      });
      const { diagnostic, mouseActionEvents } = await Promise.race([page.evaluate(({ key, targetTitle, directoryPath, phaseName, failure }) => {
        const trace = window[key];
        if (trace) {
          for (const [type, listener] of trace.listeners) document.removeEventListener(type, listener, true);
          delete window[key];
        }
        const rectOf = (element) => {
          if (!element) return null;
          const rect = element.getBoundingClientRect();
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height,
            top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left };
        };
        const nodeOf = (element) => {
          if (!element) return null;
          const style = getComputedStyle(element);
          return { tag: element.tagName.toLowerCase(), role: element.getAttribute("role"),
            aria_label: element.getAttribute("aria-label"), title: element.getAttribute("title"),
            class_name: typeof element.className === "string" ? element.className.slice(0, 180) : null,
            text: (element.textContent ?? "").trim().slice(0, 160),
            visible: style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) > 0,
            pointer_events: style.pointerEvents, rect: rectOf(element) };
        };
        const rows = [...document.querySelectorAll("[title]")]
          .filter((element) => element.getAttribute("title") === targetTitle);
        const target = rows[0] ?? null;
        const rect = rectOf(target);
        const point = rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null;
        const hitStack = [];
        for (let element = point ? document.elementFromPoint(point.x, point.y) : null;
          element && hitStack.length < 5; element = element.parentElement) {
          hitStack.push(nodeOf(element));
        }
        const menus = [...document.querySelectorAll('[role="menu"]')].slice(0, 4).map(nodeOf);
        const menuItems = [...document.querySelectorAll('[role="menuitem"]')].slice(0, 12).map(nodeOf);
        const tree = document.querySelector('[aria-label="工作区文件树"]');
        return { diagnostic: { phase: phaseName, failure, target_path: directoryPath, target_title: targetTitle,
          target_count: rows.length, target: nodeOf(target), target_aria_disabled: target?.getAttribute("aria-disabled") ?? null,
          target_disabled: target?.hasAttribute("disabled") ?? null,
          hit_test: { point, stack: hitStack }, menus, menu_items: menuItems,
          active_element: nodeOf(document.activeElement), tree_busy: tree?.getAttribute("aria-busy") ?? null,
          viewport: { width: window.innerWidth, height: window.innerHeight,
            scroll_x: window.scrollX, scroll_y: window.scrollY } }, mouseActionEvents: trace?.events ?? [] };
      }, { key: actionTraceKey, targetTitle: fileTreeTitle(directory), directoryPath: directory, phaseName: phase,
        failure: { name: error.name, message: error.message } }), diagnosticDeadline]);
      const diagnosticPath = path.join(artifacts, `menu-failure-${process.pid}-${++menuFailureSequence}.json`);
      await Promise.race([writeFile(diagnosticPath, JSON.stringify({ captured_at: new Date().toISOString(),
        ...diagnostic, mouse_action_events: mouseActionEvents }, null, 2) + "\n"), diagnosticDeadline]);
    } catch (diagnosticError) {
      process.stderr.write(`菜单失败边界诊断失败[${phase}]，保留原始异常: ${diagnosticError.message}\n`);
    } finally {
      clearTimeout(diagnosticDeadlineTimer);
    }
  };
  try {
    await row.click({ button: "right" });
  } catch (error) {
    await captureFailure("right_click_action", error);
    throw error;
  }
  try {
    await page.getByRole("menu").waitFor();
  } catch (error) {
    await captureFailure("menu_visibility_wait", error);
    throw error;
  }
  await takeActionTrace();
}
async function readCounters(page, runtime) {
  const prefix = runtime === "backend" ? "/api/v1" : "/api/gateway";
  const probe = { probe_id: `fs-${process.pid}-${++probeSequence}-${runtime}`, runtime,
    path: `${prefix}/test/navigation-measurements/fs-reads`, phases: [] };
  // 内部计数观察直连已冻结的 Gateway 目标；浏览器 mutation 仍使用量化代理。
  probe.target_url = new URL(probe.path, measurementConfig.gatewayUrl).href;
  probeTraces.push(probe);
  const start = performance.now();
  traceProbe(probe, "client_start", { target_url: probe.target_url });
  try {
    const response = await page.request.get(probe.target_url, {
      headers: { ...observationHeaders, "X-BoxTeam-Workspace-Id": measurementConfig.workspaceId,
        "X-BoxTeam-Backend-Workspace-Id": measurementConfig.backendWorkspaceId,
        "X-Test-Navigation-Probe-Id": probe.probe_id },
    });
    traceProbe(probe, "client_response", { status: response.status() });
    if (response.status() !== 200) throw new Error(`${runtime} FS 观察请求失败: ${response.status()} ${await response.text()}`);
    const snapshot = await response.json();
    if (snapshot.runtime !== runtime || !snapshot.reads
      || Object.values(snapshot.reads).some((value) => !Number.isSafeInteger(value) || value < 0)) {
      throw new Error(`FS 观察快照非法: ${JSON.stringify(snapshot)}`);
    }
    traceProbe(probe, "client_body_end");
    const responseUrl = new URL(response.url(), probe.target_url);
    observationRequests.push({ runtime, probe_id: probe.probe_id, start, end: performance.now(),
      path: responseUrl.pathname, target_url: responseUrl.href,
      status: response.status(), reads: snapshot.reads });
    return snapshot.reads;
  } catch (error) {
    traceProbe(probe, "client_error", { error: error.message.split("\n", 1)[0] });
    throw error;
  }
}
function readsInWindow(before, after) {
  const values = Object.fromEntries([...new Set([...Object.keys(before), ...Object.keys(after)])]
    .map((key) => [key, (after[key] ?? 0) - (before[key] ?? 0)]));
  if (Object.values(values).some((value) => value < 0)) throw new Error("FS 累计计数倒退，不能生成量化样本");
  return values;
}
function actualRequestMatchesSample(fact, sample, expectedDirectoryKey) {
  const actualUrl = new URL(fact.url);
  const targetUrl = new URL(fact.gateway_target_url);
  const expectedTarget = new URL(actualUrl.pathname + actualUrl.search, measurementConfig.gatewayUrl);
  const endpointByKind = { file: "entries", directory: "entries", paste: "paste", copy: "copy", upload: "upload" };
  const payload = fact.payload;
  const intent = payload?.intent;
  if (fact.method !== "POST" || actualUrl.origin !== baseUrl
    || actualUrl.pathname !== `/api/v1/workspace/files/${endpointByKind[sample.kind]}`
    || actualUrl.searchParams.get("path") !== sample.directory
    || actualUrl.searchParams.get("scope") !== "workspace"
    || targetUrl.href !== expectedTarget.href
    || fact.workspace_id_header !== measurementConfig.workspaceId || !fact.has_local_token
    || !sameDirectoryKey(intent?.directory_key, expectedDirectoryKey)) return false;
  if (sample.kind === "file" || sample.kind === "directory") {
    return payload.name === sample.name && payload.kind === (sample.kind === "directory" ? "directory" : "file");
  }
  const sourcePath = path.join(measurementConfig.workspaceRoot, "a_sources", sample.name);
  if (sample.kind === "copy") {
    return path.resolve(measurementConfig.workspaceRoot, payload.source_path) === sourcePath
      && payload.source_scope === "workspace";
  }
  if (sample.kind === "paste") {
    return payload.source_paths?.length === 1
      && path.resolve(measurementConfig.workspaceRoot, payload.source_paths[0]) === sourcePath;
  }
  return sample.kind === "upload" && payload.fileNames?.length === 1
    && payload.fileNames[0] === sample.name && payload.relativePaths?.length === 1
    && payload.relativePaths[0] === sample.name && payload.files?.length === 1
    && payload.files[0].type.split(";", 1)[0].trim().toLowerCase() === "text/plain"
    && payload.files[0].hasExpectedContent && payload.hasExpectedUploadContent;
}

async function freezeFileOperation(sample) {
  const expectedDirectoryKey = { workspace_id: measurementConfig.backendWorkspaceId, scope: "workspace",
    root: measurementConfig.workspaceRoot, path: sample.directory };
  const deadline = performance.now() + 30000;
  while (performance.now() < deadline) {
    const matches = actualMutationRequests.slice(sample.actualRequestOffset)
      .filter((fact) => actualRequestMatchesSample(fact, sample, expectedDirectoryKey));
    if (matches.length) {
      const identities = new Set(matches.map((fact) => `${fact.payload?.intent?.client_operation_id}:${fact.payload?.intent?.client_sequence}`));
      if (identities.size !== 1) throw new Error("本次 mutation 发出了多个不同 operation 意图: "
        + JSON.stringify(matches.map((fact) => ({ operation_id: fact.payload?.intent?.client_operation_id,
          client_sequence: fact.payload?.intent?.client_sequence, owner_preimage_sha256: fact.owner_preimage_sha256 }))));
      const first = matches[0];
      const intent = first.payload?.intent;
      if (!intent.client_operation_id?.match(/^op_[0-9a-f]{32}$/)
        || !Number.isSafeInteger(intent.client_sequence) || intent.client_sequence < 1) {
        throw new Error("代理实际 mutation 正文缺少 operation_id 或有效 sequence");
      }
      if (matches.some((fact) => !fact.owner_preimage_sha256
        || fact.owner_preimage_sha256 !== first.owner_preimage_sha256)) {
        throw new Error("同一 mutation 的重放 owner preimage 不一致: "
          + JSON.stringify(matches.map(actualMutationRequestSummary)));
      }
      return { operation_id: intent.client_operation_id, client_sequence: intent.client_sequence,
        directory_key: intent.directory_key, actual_url: first.url,
        gateway_target_url: first.gateway_target_url, owner_preimage: first.owner_preimage,
        owner_preimage_sha256: first.owner_preimage_sha256 };
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("代理未收到本次样本对应的唯一实际 mutation 正文: "
    + JSON.stringify({ kind: sample.kind, name: sample.name, directory: sample.directory,
      actual_request_offset: sample.actualRequestOffset }));
}

function fileMutationAttemptPairs(operation, sample) {
  const facts = actualMutationRequests.slice(sample.actualRequestOffset)
    .filter((fact) => fact.payload?.intent?.client_operation_id === operation.operation_id);
  const endpointByKind = { file: "entries", directory: "entries", paste: "paste", copy: "copy", upload: "upload" };
  const malformed = actualMutationRequests.slice(sample.actualRequestOffset).filter((fact) => {
    const url = new URL(fact.url);
    return fact.method === "POST" && url.pathname === `/api/v1/workspace/files/${endpointByKind[sample.kind]}`
      && url.searchParams.get("path") === sample.directory && url.searchParams.get("scope") === "workspace"
      && fact.payload_error;
  });
  if (malformed.length) throw new Error("样本目标目录收到无法解析的文件 mutation attempt: "
    + JSON.stringify(malformed.map(actualMutationRequestSummary)));
  if (!facts.length) return [];
  const unexpected = facts.filter((fact) => fact.payload?.intent?.client_sequence !== operation.client_sequence
    || !actualRequestMatchesSample(fact, sample, operation.directory_key));
  if (unexpected.length) throw new Error("同一样本发出了不同 operation identity 的实际请求: "
    + JSON.stringify(unexpected.map(actualMutationRequestSummary)));
  const seenBrowserRequestIds = new Set();
  const pairs = facts.map((fact) => {
    if (fact.payload_error || !fact.owner_preimage_sha256) {
      throw new Error("文件 mutation attempt 无法解析 owner preimage: " + JSON.stringify(actualMutationRequestSummary(fact)));
    }
    if (fact.owner_preimage_sha256 !== operation.owner_preimage_sha256) {
      throw new Error("同 ID 文件 operation 的 owner preimage 漂移: "
        + JSON.stringify({ operation_id: operation.operation_id, expected: operation.owner_preimage_sha256,
          actual: actualMutationRequestSummary(fact) }));
    }
    if (!fact.browser_request_id || seenBrowserRequestIds.has(fact.browser_request_id)) {
      throw new Error("每个实际文件 mutation 必须对应独立浏览器请求 ID: "
        + JSON.stringify({ operation_id: operation.operation_id,
          browser_request_id: fact.browser_request_id, proxy_exchange_id: fact.proxy_exchange_id }));
    }
    seenBrowserRequestIds.add(fact.browser_request_id);
    const browserCandidates = browserApiRequestRecords.slice(sample.requestOffset)
      .filter((record) => record.browser_request_id === fact.browser_request_id
        || (record.request && browserRequestIdByRequest.get(record.request) === fact.browser_request_id));
    if (browserCandidates.length > 1) throw new Error("一个浏览器请求 ID 对应多个 Request 事件: "
      + JSON.stringify({ operation_id: operation.operation_id, browser_request_id: fact.browser_request_id }));
    const browser = browserCandidates[0] ?? null;
    if (browser) browser.browser_request_id ??= browserRequestIdByRequest.get(browser.request) ?? null;
    if (browser && (browser.request?.url() !== fact.url || browser.method !== fact.method
      || browser.path !== fact.path)) {
      throw new Error("代理 exchange 与原始浏览器 Request 身份不一致: "
        + JSON.stringify({ browser_request_id: fact.browser_request_id,
          actual: actualMutationRequestSummary(fact), browser: { page: browser.page, path: browser.path } }));
    }
    if (fact.proxy_response_status === 409 || browser?.status === 409) {
      throw new Error("文件 operation owner 返回 409 preimage/identity 冲突: "
        + JSON.stringify({ actual: actualMutationRequestSummary(fact), browser_request_id: browser?.browser_request_id }));
    }
    // 仅完整的 owner 响应证明拒绝；代理故障或浏览器中止仍保留为未知 attempt。
    if (fact.proxy_state === "response_completed" && fact.response_body_complete
      && fact.proxy_response_status !== 202
      || browser?.browser_state === "response_completed" && browser.status !== 202
        && !fact.proxy_error) {
      const status = browser?.status ?? fact.proxy_response_status;
      throw new Error(`文件 mutation attempt 已完成但未返回 durable acceptance: ${status}; ${JSON.stringify({
          browser_request_id: fact.browser_request_id, proxy_exchange_id: fact.proxy_exchange_id,
          proxy_error: fact.proxy_error, browser_error: browser?.browser_error })}`);
    }
    if (browser?.response_error && browser.network_state === "response_completed" && !fact.proxy_error) {
      throw new Error("文件 mutation 浏览器响应观察失败: " + browser.response_error);
    }
    if (browser?.status === 202 && browser.browser_state === "response_completed"
      && (fact.proxy_response_status !== 202 || browser.proxy_exchange_id !== fact.proxy_exchange_id
        || browser.browser_request_id !== fact.browser_request_id)) {
      throw new Error("完成的浏览器 202 未与同一代理 exchange 配对: "
        + JSON.stringify({ actual: actualMutationRequestSummary(fact), browser: {
          browser_request_id: browser.browser_request_id, proxy_exchange_id: browser.proxy_exchange_id,
          status: browser.status } }));
    }
    if (browser?.status === 202 && browser.browser_state === "response_completed" && !browser.receipt) {
      throw new Error("完整的浏览器 202 缺少 durable acceptance 回执: " + fact.proxy_exchange_id);
    }
    if (browser?.response_observation_state === "completed"
      && browser.status === 202 && fact.proxy_response_status === 202
      && browser.proxy_exchange_id === fact.proxy_exchange_id
      && browser.browser_request_id === fact.browser_request_id && browser.receipt) {
      const receipt = browser.receipt;
      if (receipt.operation_id !== operation.operation_id
        || receipt.client_sequence !== operation.client_sequence
        || !Number.isSafeInteger(receipt.queue_seq) || receipt.queue_seq < 1
        || !Number.isSafeInteger(receipt.receipt_revision) || receipt.receipt_revision < 1) {
        throw new Error("文件 202 回执的 operation/sequence/queue_seq/revision 非法: "
          + JSON.stringify({ receipt, operation_id: operation.operation_id,
            client_sequence: operation.client_sequence, proxy_exchange_id: fact.proxy_exchange_id }));
      }
      if (operation.accepted_queue_seq !== undefined && operation.accepted_queue_seq !== receipt.queue_seq) {
        throw new Error("同一文件 operation 的 202 回执 queue_seq 漂移: "
          + JSON.stringify({ expected: operation.accepted_queue_seq, receipt }));
      }
      operation.accepted_queue_seq = receipt.queue_seq;
    }
    return { fact, browser };
  });
  const receipts = pairs.flatMap(({ fact, browser }) => browser?.response_observation_state === "completed"
    && browser.status === 202 && fact.proxy_response_status === 202
    && browser.proxy_exchange_id === fact.proxy_exchange_id
    && browser.browser_request_id === fact.browser_request_id && browser.receipt
    ? [{ exchange: fact.proxy_exchange_id, receipt: browser.receipt }] : []);
  const byExchange = new Map(receipts.map((value) => [value.exchange, value.receipt]));
  const orderedReceipts = [...byExchange.values()].sort((left, right) => left.receipt_revision - right.receipt_revision);
  const stateOrder = { queued: 0, running: 1, committed: 2, rejected: 2,
    cancelled: 2, dependency_failed: 2 };
  for (let index = 1; index < orderedReceipts.length; index += 1) {
    const previous = orderedReceipts[index - 1];
    const current = orderedReceipts[index];
    if (current.queue_seq !== previous.queue_seq
      || current.receipt_revision < previous.receipt_revision
      || current.receipt_revision === previous.receipt_revision && current.state !== previous.state
      || (stateOrder[current.state] ?? 0) < (stateOrder[previous.state] ?? 0)) {
      throw new Error("重放接受回执的 queue_seq/state/revision 未保持单调: "
        + JSON.stringify(orderedReceipts));
    }
  }
  return pairs;
}

function summarizeFileMutationAttempts(operation, sample, requireAccepted = true) {
  const pairs = fileMutationAttemptPairs(operation, sample);
  const accepted = pairs.filter(({ fact, browser }) => browser?.status === 202
    && fact.proxy_response_status === 202 && browser.proxy_exchange_id === fact.proxy_exchange_id
    && browser.browser_request_id === fact.browser_request_id && browser.receipt
    && fact.proxy_state === "response_completed" && browser.browser_state === "response_completed");
  if (requireAccepted && !accepted.length) throw new Error("样本结束时没有保留原始 Request/Response 与 proxy exchange 配对的 202: "
    + JSON.stringify(pairs.map(({ fact, browser }) => ({
      browser_request_id: fact.browser_request_id, proxy_exchange_id: fact.proxy_exchange_id,
      proxy_state: fact.proxy_state, proxy_response_status: fact.proxy_response_status,
      browser_state: browser?.browser_state ?? "request_event_missing", browser_status: browser?.status ?? null,
      browser_error: browser?.browser_error ?? null,
    }))));
  return pairs.map(({ fact, browser }, index) => ({
    attempt_index: index + 1,
    attempt_count: pairs.length,
    browser_request_id: fact.browser_request_id,
    proxy_exchange_id: fact.proxy_exchange_id,
    proxy_state: fact.proxy_state,
    proxy_response_status: fact.proxy_response_status,
    proxy_error: fact.proxy_error,
    browser_page: browser?.page ?? null,
    browser_state: browser?.browser_state ?? "request_event_missing",
    browser_network_state: browser?.network_state ?? "request_event_missing",
    response_observation_state: browser?.response_observation_state ?? "request_event_missing",
    browser_status: browser?.status ?? null,
    browser_error: browser?.browser_error ?? null,
    response_error: browser?.response_error ?? null,
    request_id: browser?.request_id ?? null,
    backend_fs_reads: browser?.backend_reads ?? null,
    receipt: browser?.receipt ? { operation_id: browser.receipt.operation_id,
      client_sequence: browser.receipt.client_sequence, queue_seq: browser.receipt.queue_seq,
      state: browser.receipt.state, receipt_revision: browser.receipt.receipt_revision } : null,
    outcome: accepted.some((entry) => entry.fact === fact) ? "accepted"
      : "unknown_or_aborted",
    raw_body_sha256: fact.raw_body_sha256,
    owner_preimage: fact.owner_preimage,
    owner_preimage_sha256: fact.owner_preimage_sha256,
  }));
}

async function waitForFileAcceptance(operation, sample) {
  const deadline = performance.now() + 60000;
  while (performance.now() < deadline) {
    const pairs = fileMutationAttemptPairs(operation, sample);
    for (const { fact, browser } of pairs) {
      if (fact.proxy_state === "response_completed" && fact.proxy_response_status === 202
        && browser?.browser_state === "response_completed" && browser.status === 202
        && browser.proxy_exchange_id === fact.proxy_exchange_id
        && browser.browser_request_id === fact.browser_request_id && browser.receipt?.operation_id === operation.operation_id) {
        return { actual: fact, browser, accepted: browser.receipt };
      }
    }
    await waitForMeasurementPageDelay(sample.page, 20);
  }
  throw new Error("等待任一已 attach 页面按冻结 operation/exchange 配对实际请求与 202 超时: "
    + JSON.stringify({ operation_id: operation.operation_id,
      attempts: summarizeFileMutationAttempts(operation, sample, false) }));
}

async function captureFileUiBaselines() {
  return Promise.all(context.pages().filter((page) => pageIds.has(page) && !page.isClosed()).map(async (page) => {
    const baseline = await page.evaluate(() => {
      const status = document.getElementById("file-status");
      if (!status || !window.navigationHarness?.observerReady) throw new Error("已attach页面缺少已就绪的文件UI持续观察器");
      return { completed_before: Number(status.dataset.completedCount),
        event_offset: window.navigationHarness.fileStatusEvents.length,
        snapshot_offset: window.navigationHarness.fileUiSnapshotOrder.length };
    });
    return { page, page_id: pageIds.get(page), ...baseline };
  }));
}

function fileUiCompletionSnapshot(state) {
  const harness = window.navigationHarness;
  const operationId = state.operation.operation_id;
  const indexedSnapshot = harness.fileUiSnapshots[operationId] ?? null;
  const snapshot = indexedSnapshot && (!state.diagnostic
    || (indexedSnapshot.operation_id === operationId
      && indexedSnapshot.client_sequence === state.operation.client_sequence)) ? indexedSnapshot : null;
  const rejectionReasons = (candidate) => {
    const event = state.candidate_event ?? candidate.event;
    const eventIndex = state.candidate_event_index ?? candidate.event_index;
    const checks = [
      ["observation_source", candidate.observation_source === "mounted_mutation_observer"],
      ["operation_id", candidate.operation_id === operationId],
      ["client_sequence", candidate.client_sequence === state.operation.client_sequence],
      ["queue_seq", state.operation.accepted_queue_seq === undefined
        || candidate.queue_seq === state.operation.accepted_queue_seq],
      ["receipt_revision", state.operation.receipt_revision === undefined
        || candidate.receipt_revision === state.operation.receipt_revision],
      ["event_index_matches_snapshot", eventIndex === candidate.event_index],
      ["event_after_baseline", candidate.event_index >= state.baseline.event_offset],
      ["snapshot_after_baseline", candidate.snapshot_index >= state.baseline.snapshot_offset],
      ["completion_count_after_baseline", candidate.completed_count > state.baseline.completed_before],
      ["completion_message", candidate.message === "文件操作已完成"],
      ["target_row_visible", candidate.target_row_visible === true],
      ["target_path_title", candidate.target_path_title === state.target_path_title],
      ["event_message", event?.message === candidate.message],
      ["event_attribution", !event?.attribution_error],
      ["event_ordinal_matches_snapshot", event?.ordinal === candidate.event?.ordinal],
      ["event_completion_count", event?.completed_count === candidate.completed_count],
      ["terminal_operation_id", event?.terminal?.operation_id === candidate.operation_id],
      ["terminal_client_sequence", event?.terminal?.client_sequence === candidate.client_sequence],
      ["terminal_queue_seq", event?.terminal?.queue_seq === candidate.queue_seq],
      ["terminal_receipt_revision", event?.terminal?.receipt_revision === candidate.receipt_revision],
      ["terminal_state", event?.terminal?.state === "committed"],
      ["terminal_effect", event?.terminal?.effect === "complete"],
      ["terminal_precedes_status_event", event?.terminal?.ordinal < event?.ordinal],
      ["terminal_time_precedes_status_event", event?.terminal?.at_ms <= event?.at_ms],
    ];
    return checks.filter(([, passed]) => !passed).map(([condition]) => condition);
  };
  if (!state.diagnostic) {
    if (!snapshot || rejectionReasons(snapshot).length) return null;
    return snapshot;
  }

  const baseline = state.baseline;
  const relatedSnapshots = harness.fileUiSnapshotOrder.filter((candidate) =>
    candidate.snapshot_index >= baseline.snapshot_offset
    && candidate.operation_id === operationId
    && candidate.client_sequence === state.operation.client_sequence
    && candidate.event?.terminal?.operation_id === operationId
    && candidate.event?.terminal?.client_sequence === state.operation.client_sequence);
  const snapshotCandidates = [...new Map([
    ...(snapshot ? [[snapshot.snapshot_index, snapshot]] : []),
    ...relatedSnapshots.map((candidate) => [candidate.snapshot_index, candidate]),
  ]).values()].slice(-8);
  const eventEntries = harness.fileStatusEvents.map((event, eventIndex) => ({ event, event_index: eventIndex }))
    .filter(({ event_index }) => event_index >= baseline.event_offset);
  const events = eventEntries.filter(({ event }) => event.terminal?.operation_id === operationId
    && event.terminal?.client_sequence === state.operation.client_sequence)
    .slice(-12).map(({ event, event_index }) => ({ event_index, ...event }));
  const matchingTerminals = harness.pendingTerminals
    .filter((terminal) => terminal.operation_id === operationId
      && terminal.client_sequence === state.operation.client_sequence);
  const tree = document.querySelector("[role=tree]");
  const rows = [...(tree?.querySelectorAll("[title]") ?? [])]
    .filter((element) => element.getAttribute("title") === state.target_path_title);
  const row = rows[0] ?? null;
  const scroller = row?.closest("[data-virtuoso-scroller]") ?? tree;
  const rectOf = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left };
  };
  const rowRect = rectOf(row);
  const scrollerRect = rectOf(scroller);
  const style = row ? getComputedStyle(row) : null;
  const rowRejections = [];
  if (!tree) rowRejections.push("tree_not_mounted");
  if (!row?.isConnected) rowRejections.push("target_row_not_mounted");
  if (style?.display === "none") rowRejections.push("target_row_display_none");
  if (style?.visibility === "hidden") rowRejections.push("target_row_visibility_hidden");
  if (rowRect && rowRect.width <= 0) rowRejections.push("target_row_width_zero");
  if (rowRect && rowRect.height <= 0) rowRejections.push("target_row_height_zero");
  if (rowRect && scrollerRect && rowRect.top < scrollerRect.top) rowRejections.push("target_row_above_viewport");
  if (rowRect && scrollerRect && rowRect.bottom > scrollerRect.bottom) rowRejections.push("target_row_below_viewport");
  const snapshotsAfterBaseline = harness.fileUiSnapshotOrder.slice(baseline.snapshot_offset);
  return {
    operation_snapshot_present: snapshot !== null,
    operation_snapshot_rejection_conditions: snapshot ? rejectionReasons(snapshot) : null,
    operation_snapshot: snapshot,
    related_snapshot_count: relatedSnapshots.length,
    related_snapshots: snapshotCandidates.map((candidate) => ({
      snapshot_index: candidate.snapshot_index, event_index: candidate.event_index,
      operation_id: candidate.operation_id, client_sequence: candidate.client_sequence,
      queue_seq: candidate.queue_seq, receipt_revision: candidate.receipt_revision,
      completed_count: candidate.completed_count, message: candidate.message,
      target_path_title: candidate.target_path_title, target_row_visible: candidate.target_row_visible,
      event_terminal_operation_id: candidate.event?.terminal?.operation_id ?? null,
      rejection_conditions: rejectionReasons(candidate),
    })),
    status: { message: document.getElementById("file-status")?.textContent ?? null,
      completed_count: Number(document.getElementById("file-status")?.dataset.completedCount ?? 0) },
    matching_completion_event_count: events.length,
    matching_completion_events: events,
    pending_terminal_count: matchingTerminals.length,
    pending_terminals: matchingTerminals,
    file_ui_observer_diagnostics: harness.fileUiDiagnosticObservations.filter((diagnostic) =>
      diagnostic.operation_id === operationId
      && diagnostic.client_sequence === state.operation.client_sequence).slice(-12),
    target_row: { tree_mounted: Boolean(tree), matching_rows: rows.length,
      mounted: Boolean(row), connected: Boolean(row?.isConnected),
      display: style?.display ?? null, visibility: style?.visibility ?? null,
      rect: rowRect, viewport: scrollerRect,
      visible_under_existing_rule: rowRejections.length === 0,
      rejection_conditions: rowRejections,
      scroll: { window_x: window.scrollX, window_y: window.scrollY,
        document_y: document.documentElement.scrollTop,
        scroller_top: scroller?.scrollTop ?? null, scroller_height: scroller?.clientHeight ?? null,
        scroller_scroll_height: scroller?.scrollHeight ?? null } },
    snapshot_order: { total: harness.fileUiSnapshotOrder.length,
      baseline_offset: baseline.snapshot_offset, after_baseline: snapshotsAfterBaseline.length,
      last: harness.fileUiSnapshotOrder.slice(-8).map((candidate) => ({
        snapshot_index: candidate.snapshot_index, event_index: candidate.event_index,
        operation_id: candidate.operation_id, client_sequence: candidate.client_sequence,
        target_path_title: candidate.target_path_title,
      })) },
  };
}

function fileOperationObservations(operation, sample) {
  return browserApiRequestRecords.slice(sample.requestOffset).flatMap((record) => {
    if (record.response_error && record.network_state === "response_completed") {
      const fact = actualMutationRequests.find((value) => value.browser_request_id === record.browser_request_id);
      if (!fact?.proxy_error) throw new Error(record.response_error);
    }
    if (record.browser_state !== "response_completed") return [];
    if (record.operation_status?.worker_state === "failed") {
      throw new Error("文件 worker 已停止: " + record.operation_status.worker_error);
    }
    const receipts = record.receipt ? [record.receipt] : record.operation_status?.operations ?? [];
    return receipts.filter((receipt) => receipt.operation_id === operation.operation_id)
      .map((receipt) => ({ record, receipt }));
  });
}

async function waitForFileUiCompletion(operation, sample) {
  const deadline = sample.start + 30000;
  const targetTitle = fileTreeTitle(path.join(sample.directory, sample.name));
  const candidates = sample.completionBaselines.filter((baseline) => !sample.requiredConfirmationPage
    || baseline.page_id === sample.requiredConfirmationPage);
  if (!candidates.length) throw new Error("冻结operation没有可用的确认页DOM观察基线");
  while (performance.now() < deadline) {
    for (const baseline of candidates) {
      const snapshot = await baseline.page.evaluate(fileUiCompletionSnapshot, {
        operation,
        baseline: { completed_before: baseline.completed_before, event_offset: baseline.event_offset,
          snapshot_offset: baseline.snapshot_offset },
        target_path_title: targetTitle,
      });
      if (!snapshot) continue;
      if (snapshot.browser_epoch_ms !== snapshot.browser_time_origin_ms + snapshot.browser_now_ms
        || !Number.isFinite(snapshot.browser_epoch_ms) || snapshot.browser_epoch_ms < 0
        || snapshot.target_path_title !== targetTitle) {
        throw new Error("DOM快照的Chromium时间域非法");
      }
      return { snapshot, page: baseline.page, page_id: baseline.page_id, baseline };
    }
    await waitForMeasurementPageDelay(sample.page, 20);
  }
  const failure = new Error("从原动作30s期限内未消费到冻结operation的真实DOM双帧快照: " + operation.operation_id);
  let diagnosticTimer;
  const diagnostic = { captured_at_utc: new Date().toISOString(), operation: {
    operation_id: operation.operation_id, client_sequence: operation.client_sequence,
    accepted_queue_seq: operation.accepted_queue_seq ?? null }, sample: {
    start_time_origin_ms: sample.start_time_origin_ms, start_now_ms: sample.start_now_ms,
    start_epoch_ms: sample.start_epoch_ms, target_path_title: targetTitle,
    baseline_pages: candidates.map((baseline) => ({ page_id: baseline.page_id,
      completed_before: baseline.completed_before, event_offset: baseline.event_offset,
      snapshot_offset: baseline.snapshot_offset })) }, pages: [] };
  try {
    const capture = Promise.all(candidates.map(async (baseline) => ({ page_id: baseline.page_id,
      ...await baseline.page.evaluate(fileUiCompletionSnapshot, {
        operation: { operation_id: operation.operation_id, client_sequence: operation.client_sequence,
          accepted_queue_seq: operation.accepted_queue_seq },
        baseline: { completed_before: baseline.completed_before, event_offset: baseline.event_offset,
          snapshot_offset: baseline.snapshot_offset }, target_path_title: targetTitle, diagnostic: true,
      }) })));
    const deadline = new Promise((_, reject) => {
      diagnosticTimer = setTimeout(() => reject(new Error("文件UI失败诊断超过3000ms总期限")), 3000);
    });
    diagnostic.pages = await Promise.race([capture, deadline]);
  } catch (error) {
    diagnostic.diagnostic_error = error.message;
    process.stderr.write(`文件UI超时诊断失败，保留原始异常: ${error.message}\n`);
  } finally {
    clearTimeout(diagnosticTimer);
  }
  diagnostic.playwright_node_phase_times = browserApiRequestRecords.slice(sample.requestOffset)
    .filter((record) => record.request && actualMutationRequests.some((fact) =>
      fact.browser_request_id === record.browser_request_id
      && fact.payload?.intent?.client_operation_id === operation.operation_id))
    .map((record) => ({ browser_request_id: record.browser_request_id,
      proxy_exchange_id: record.proxy_exchange_id ?? null, method: record.method, path: record.path,
      status: record.status, browser_state: record.browser_state, network_state: record.network_state,
      response_observation_state: record.response_observation_state,
      phases: record.node_phase_times }));
  diagnostic.proxy_node_phase_times = actualMutationRequests
    .filter((fact) => fact.payload?.intent?.client_operation_id === operation.operation_id)
    .map((fact) => ({ browser_request_id: fact.browser_request_id,
      proxy_exchange_id: fact.proxy_exchange_id, phases: fact.proxy_node_phase_times }));
  fileUiFailureDiagnostics.push(diagnostic);
  throw failure;
}

function beginFileUiCompletion(operation, sample) {
  const promise = waitForFileUiCompletion(operation, sample);
  void promise.catch(() => undefined);
  sample.uiCompletionPromise = promise;
  const targetPath = path.join(sample.directory, sample.name);
  const initiatingPathPromise = waitForActualVisibleTreeRow(sample.page, targetPath, sample.start + 30000);
  void initiatingPathPromise.catch(() => undefined);
  sample.initiatingPathPromise = initiatingPathPromise;
  const windowPromise = promise.then(async ({ snapshot, page }) => {
    const timer = await page.evaluate(async ({ endpoint, duration }) => {
      const deadline = endpoint + duration;
      let observed = performance.timeOrigin + performance.now();
      while (observed < deadline) {
        const delayMs = Math.max(1, Math.ceil(deadline - observed));
        await new Promise((resolve) => setTimeout(resolve, delayMs));
        observed = performance.timeOrigin + performance.now();
      }
      return { deadline_epoch_ms: deadline,
        observed_epoch_ms: observed };
    }, { endpoint: snapshot.browser_epoch_ms, duration: 650 });
    const readsAfter = await readCounters(sample.page, "backend");
    return { readsAfter, endpoint_epoch_ms: snapshot.browser_epoch_ms,
      deadline_epoch_ms: timer.deadline_epoch_ms, timer_observed_epoch_ms: timer.observed_epoch_ms,
      observed_epoch_ms: performance.timeOrigin + performance.now() };
  });
  void windowPromise.catch(() => undefined);
  sample.fsWindowPromise = windowPromise;
  return promise;
}

function assertFileUiCompletionIdentity(snapshot, operation, sample, accepted, terminal, pageId) {
  const event = snapshot.event;
  const baseline = sample.completionBaselines.find((value) => value.page_id === pageId);
  const matches = snapshot.observation_source === "mounted_mutation_observer"
    && snapshot.operation_id === operation.operation_id
    && snapshot.client_sequence === operation.client_sequence
    && snapshot.queue_seq === accepted.queue_seq && snapshot.receipt_revision === terminal.receipt_revision
    && baseline !== undefined && snapshot.event_index >= baseline.event_offset
    && snapshot.snapshot_index >= baseline.snapshot_offset
    && snapshot.completed_count > baseline.completed_before
    && snapshot.message === "文件操作已完成" && snapshot.target_row_visible
    && event.message === snapshot.message && !event.attribution_error
    && event.terminal?.operation_id === operation.operation_id
    && event.terminal?.client_sequence === operation.client_sequence
    && event.terminal?.queue_seq === accepted.queue_seq
    && event.terminal?.receipt_revision === terminal.receipt_revision
    && event.terminal?.state === "committed" && event.terminal?.effect === "complete"
    && event.completed_count === snapshot.completed_count && event.terminal.ordinal < event.ordinal
    && event.terminal.at_ms <= event.at_ms;
  if (!matches) throw new Error("冻结DOM快照未与原operation、队列和终态revision精确配对: "
    + JSON.stringify({ operation, accepted, terminal_revision: terminal.receipt_revision, snapshot }));
}

async function finishFileMutation(page, operation, sample) {
  const acceptedPair = await waitForFileAcceptance(operation, sample);
  const { actual: acceptedRequest, browser: browserRequestRecord, accepted } = acceptedPair;
  if (accepted.operation_id !== operation.operation_id
    || acceptedRequest.payload?.intent?.client_sequence !== operation.client_sequence
    || acceptedRequest.proxy_response_status !== 202
    || browserRequestRecord.proxy_exchange_id !== acceptedRequest.proxy_exchange_id) {
    throw new Error("文件 operation 回执与代理实际正文及同请求响应身份不一致: "
      + JSON.stringify({ operation, accepted, request: actualMutationRequestSummary(acceptedRequest) }));
  }
  browserRequestRecord.actual_request = actualMutationRequestSummary(acceptedRequest);
  const responseUrl = browserRequestRecord.request.url();
  const requestUrl = new URL(responseUrl);
  const actualUrl = new URL(acceptedRequest.url);
  const expectedGatewayUrl = new URL(actualUrl.pathname + actualUrl.search, measurementConfig.gatewayUrl);
  const observedGatewayUrl = new URL(acceptedRequest.gateway_target_url);
  const expectedDirectoryKey = { workspace_id: measurementConfig.backendWorkspaceId, scope: "workspace",
    root: measurementConfig.workspaceRoot, path: sample.directory };
  const requestPayload = acceptedRequest.payload;
  const requestIntent = requestPayload?.intent;
  const endpointByKind = { file: "entries", directory: "entries", paste: "paste", copy: "copy", upload: "upload" };
  if (requestUrl.origin !== baseUrl || actualUrl.pathname !== "/api/v1/workspace/files/" + endpointByKind[sample.kind]
    || actualUrl.searchParams.get("path") !== sample.directory
    || actualUrl.searchParams.get("scope") !== expectedDirectoryKey.scope
    || observedGatewayUrl.href !== expectedGatewayUrl.href
    || acceptedRequest.workspace_id_header !== measurementConfig.workspaceId || !acceptedRequest.has_local_token
    || !sameDirectoryKey(requestIntent?.directory_key, operation.directory_key)
    || !sameDirectoryKey(requestIntent?.directory_key, expectedDirectoryKey)
    || requestIntent.client_operation_id !== operation.operation_id
    || requestIntent.client_sequence !== operation.client_sequence || responseUrl !== acceptedRequest.url
    || operation.actual_url !== acceptedRequest.url || operation.gateway_target_url !== acceptedRequest.gateway_target_url) {
    throw new Error("文件 mutation 跨页请求目标或凭据与冻结样本不一致: "
      + JSON.stringify({ kind: sample.kind, operation_id: operation.operation_id,
        browser_url: responseUrl, actual_url: acceptedRequest.url, gateway_target: expectedGatewayUrl.href,
        workspace_id_header: acceptedRequest.workspace_id_header, has_local_token: acceptedRequest.has_local_token,
        request_directory_key: requestIntent?.directory_key, expectedDirectoryKey }));
  }
  const sourcePath = path.join(measurementConfig.workspaceRoot, "a_sources", sample.name);
  if ((sample.kind === "file" || sample.kind === "directory")
    && (requestPayload.name !== sample.name || requestPayload.kind !== (sample.kind === "directory" ? "directory" : "file"))) {
    throw new Error("创建请求内容与目标样本不一致: " + JSON.stringify(requestPayload));
  }
  if (sample.kind === "copy" && (path.resolve(measurementConfig.workspaceRoot, requestPayload.source_path) !== sourcePath
    || requestPayload.source_scope !== "workspace")) {
    throw new Error("复制请求来源与样本不一致: " + JSON.stringify(requestPayload));
  }
  if (sample.kind === "paste" && (requestPayload.source_paths?.length !== 1
    || path.resolve(measurementConfig.workspaceRoot, requestPayload.source_paths[0]) !== sourcePath)) {
    throw new Error("粘贴请求来源与样本不一致: " + JSON.stringify(requestPayload));
  }
  if (sample.kind === "upload" && (requestPayload.fileNames?.length !== 1
    || requestPayload.fileNames[0] !== sample.name || requestPayload.relativePaths?.length !== 1
    || requestPayload.relativePaths[0] !== sample.name || requestPayload.files?.length !== 1
    || requestPayload.files[0].type.split(";", 1)[0].trim().toLowerCase() !== "text/plain"
    || !requestPayload.files[0].hasExpectedContent || !requestPayload.hasExpectedUploadContent)) {
    throw new Error("上传请求正文与样本不一致: " + JSON.stringify(requestPayload));
  }
  const deadline = performance.now() + 60000;
  let terminal;
  while (!terminal) {
    fileMutationAttemptPairs(operation, sample);
    const observed = fileOperationObservations(operation, sample).map(({ receipt }) => receipt);
    if (observed.some((receipt) => receipt.client_sequence !== operation.client_sequence
      || receipt.queue_seq !== accepted.queue_seq
      || !Number.isSafeInteger(receipt.receipt_revision) || receipt.receipt_revision < 1)) {
      throw new Error("原文件 operation 的完成观察回执 identity/queue_seq/revision 不一致: "
        + JSON.stringify(observed));
    }
    const latest = [accepted, ...observed].sort((left, right) => right.receipt_revision - left.receipt_revision)[0];
    if (["committed", "rejected", "cancelled", "dependency_failed"].includes(latest.state)) {
      if (latest.state !== "committed" || latest.effect !== "complete") {
        throw new Error("原文件 operation 未完整提交: " + JSON.stringify(latest));
      }
      terminal = latest;
    } else {
      if (performance.now() >= deadline) throw new Error("等待任一已 attach 页面确认原文件 operation 完成超时: "
        + operation.operation_id);
      await waitForMeasurementPageDelay(page, 20);
    }
  }
  const relative = sample.directory + "/" + sample.name;
  const itemKind = sample.kind === "directory" ? "directory" : "file";
  const expectedOperationKind = sample.kind === "file" || sample.kind === "directory" ? "create" : sample.kind;
  const items = terminal.items.filter((value) => value.path === sample.name && value.kind === itemKind);
  const directories = terminal.directories.filter((value) => sameDirectoryKey(value.directory_key, expectedDirectoryKey));
  const snapshots = terminal.snapshots.filter((value) => sameDirectoryKey(value.directory_key, expectedDirectoryKey)
    && value.root_path === measurementConfig.workspaceRoot && value.path === sample.directory);
  const snapshot = snapshots[0];
  const snapshotItems = snapshot?.items.filter((value) => value.path === relative && value.kind === itemKind) ?? [];
  const expectedCreatedPath = expectedOperationKind === "create" ? relative : null;
  if (terminal.kind !== expectedOperationKind || items.length !== 1 || items[0].state !== "published"
    || directories.length !== 1 || !directories[0].exists || snapshots.length !== 1
    || snapshot.directory_revision !== directories[0].directory_revision || snapshotItems.length !== 1
    || (expectedCreatedPath !== null && terminal.created_path !== expectedCreatedPath)
    || (terminal.created_path && terminal.created_path !== relative)) {
    throw new Error("committed 回执未证明原请求目标下的发布项与同目标快照: "
      + JSON.stringify({ terminal, expectedDirectoryKey, expectedCreatedPath }));
  }
  const physicalPath = path.join(measurementConfig.workspaceRoot, relative);
  const physical = await lstat(physicalPath);
  if (sample.kind === "directory" ? !physical.isDirectory() : !physical.isFile()) {
    throw new Error("文件 operation 物理发布类型不一致: " + physicalPath);
  }
  const bytes = physical.isFile() ? await readFile(physicalPath) : null;
  const expectedContent = sample.kind === "upload" ? "正式量化上传内容\n"
    : ["copy", "paste"].includes(sample.kind) ? "正式量化复制内容\n" : "";
  if (bytes && bytes.toString("utf8") !== expectedContent) throw new Error("物理发布正文不一致: " + physicalPath);
  const confirmed = await sample.uiCompletionPromise;
  const uiCompletion = { ...confirmed.snapshot, page_id: confirmed.page_id,
    baseline_completed_before: confirmed.baseline.completed_before,
    baseline_event_offset: confirmed.baseline.event_offset,
    baseline_snapshot_offset: confirmed.baseline.snapshot_offset,
    observed_dom_text: confirmed.snapshot.message,
    observed_dom_completed_count: confirmed.snapshot.completed_count };
  assertFileUiCompletionIdentity(uiCompletion, operation, sample, accepted, terminal, confirmed.page_id);
  const initiatingUiPathVisible = await sample.initiatingPathPromise;
  const fsWindow = await sample.fsWindowPromise;
  const nodeClockAfter = nodeClockSample();
  const elapsedMs = uiCompletion.browser_epoch_ms - sample.start_epoch_ms;
  if (!Number.isFinite(elapsedMs) || elapsedMs < 0) {
    throw new Error("文件 mutation从菜单动作到真实DOM双帧确认的时长非法: "
      + JSON.stringify({ elapsed_ms: elapsedMs, start_epoch_ms: sample.start_epoch_ms,
        endpoint_epoch_ms: uiCompletion.browser_epoch_ms, node_time_origin_ms: sample.start_time_origin_ms,
        node_now_ms: sample.start_now_ms, browser_time_origin_ms: uiCompletion.browser_time_origin_ms,
        browser_now_ms: uiCompletion.browser_now_ms }));
  }
  const fsWindowLengthMs = fsWindow.observed_epoch_ms - fsWindow.endpoint_epoch_ms;
  const initiatingUiState = await page.locator("#file-status").evaluate((element) => ({
    message: element.textContent, completed_count: Number(element.dataset.completedCount),
  }));
  const fileStatus = uiCompletion.message;
  if (fileStatus !== "文件操作已完成") throw new Error("确认页未观察到对应operation的完成回调: "
    + JSON.stringify({ operation_id: operation.operation_id, ui_completion: uiCompletion }));
  return { sample, facts: { elapsed_ms: elapsedMs,
    name: sample.name, kind: sample.kind, directory: sample.directory, operation_id: operation.operation_id,
    client_sequence: operation.client_sequence, owner_preimage: operation.owner_preimage,
    owner_preimage_sha256: operation.owner_preimage_sha256, directory_key: operation.directory_key,
    accepted_state: accepted.state, accepted_queue_seq: accepted.queue_seq,
    terminal_state: terminal.state, terminal_effect: terminal.effect,
    receipt_revision: terminal.receipt_revision, directory_revision: directories[0].directory_revision,
    target_directory_key: expectedDirectoryKey, request_directory_key: requestIntent.directory_key,
    receipt_item_path: items[0].path, receipt_item_kind: items[0].kind,
    snapshot_path: snapshot.path, snapshot_item_path: snapshotItems[0].path,
    created_path: terminal.created_path, request_has_local_token: acceptedRequest.has_local_token,
    request_workspace_id: acceptedRequest.workspace_id_header, physical_path: physicalPath,
    initiating_page: pageIds.get(page), dispatch_page: browserRequestRecord.page,
    confirmation_page: confirmed.page_id,
    cross_page_dispatched: pageIds.get(page) !== browserRequestRecord.page,
    cross_page_confirmed: pageIds.get(page) !== confirmed.page_id,
    initiating_ui_path_visible: initiatingUiPathVisible,
    initiating_ui_state_after_completion_diagnostic: initiatingUiState,
    confirmation_ui_completed: true, ui_completion: uiCompletion,
    ui_elapsed_clock: { node: { time_origin_ms: sample.start_time_origin_ms,
        start_now_ms: sample.start_now_ms, start_epoch_ms: sample.start_epoch_ms,
        observed_after_snapshot: nodeClockAfter },
      browser: { time_origin_ms: uiCompletion.browser_time_origin_ms,
        endpoint_now_ms: uiCompletion.browser_now_ms, endpoint_epoch_ms: uiCompletion.browser_epoch_ms },
      elapsed_ms: elapsedMs },
    fs_window: { endpoint_epoch_ms: fsWindow.endpoint_epoch_ms,
      deadline_epoch_ms: fsWindow.deadline_epoch_ms,
      timer_observed_epoch_ms: fsWindow.timer_observed_epoch_ms,
      observed_epoch_ms: fsWindow.observed_epoch_ms, length_ms: fsWindowLengthMs,
      reads_after: fsWindow.readsAfter },
    physical_sha256: bytes ? createHash("sha256").update(bytes).digest("hex") : null } };
}
async function createViaMenu(page, name, kind, directory = "d000", observationStart = null) {
  if (!observationStart?.prepared) await prepareFileMutationMenu(page, directory);
  page.once("dialog", (dialog) => dialog.accept(name));
  const requestOffset = observationStart?.requestOffset ?? browserApiRequestRecords.length;
  const pageRequestOffset = pageRequests.get(page).length;
  const actualRequestOffset = observationStart?.actualRequestOffset ?? actualMutationRequests.length;
  const readsBefore = await readCounters(page, "backend");
  const completionBaselines = observationStart?.completionBaselines ?? await captureFileUiBaselines();
  const startTimeOriginMs = performance.timeOrigin;
  const start = performance.now();
  const startEpochMs = startTimeOriginMs + start;
  await page.getByRole("menuitem", { name: kind === "file" ? "新建文件" : "新建文件夹", exact: true }).click();
  const sample = { start, start_time_origin_ms: startTimeOriginMs, start_now_ms: start,
    start_epoch_ms: startEpochMs, requestOffset, pageRequestOffset, actualRequestOffset, readsBefore, completionBaselines,
    requiredConfirmationPage: observationStart?.confirmationPages?.get(pageIds.get(page)), name, kind, directory, page };
  const operation = await freezeFileOperation(sample);
  beginFileUiCompletion(operation, sample);
  return await finishFileMutation(page, operation, sample);
}
const sumFileSystemReadCounts = (readCounts) => Object.values(readCounts ?? {}).reduce((total, count) => total + count, 0);
const calculateP95 = (values) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * 0.95) - 1];
async function measureMutations() {
  for (const expanded of (focusConcurrent ? [] : minimalSamples ? [1] : [1, 20, 200])) {
    const page = await context.newPage();
    const pageId = attachMeasurementPageObservers(page);
    if (!clockAlignmentSamples.some((sample) => sample.label === "run_before")) {
      await captureClockAlignment(page, "run_before");
    }
    await openReadyMeasurementPage(page, expanded);
    const kinds = ["file", "directory", "paste", "copy", "upload"];
    const sampleOrder = minimalSamples ? kinds.map((kind) => ({ kind, index: 0 })) : [
      ...kinds.map((kind) => ({ kind, index: 0 })),
      ...kinds.flatMap((kind) => Array.from({ length: 19 }, (_, index) => ({ kind, index: index + 1 }))),
    ];
    for (const { kind, index } of sampleOrder) {
        const name = `${kind}_${expanded}_${String(index).padStart(3, "0")}.txt`;
        let timing;
        if (kind === "file" || kind === "directory") {
          timing = await createViaMenu(page, name, kind);
        } else {
          if (kind === "copy") {
            await contextMenu(page, `a_sources/${name}`);
            await page.getByRole("menuitem", { name: /^复制\s+Ctrl\+C$/ }).click();
          } else if (kind === "paste") {
            await visibleTreePath(page, `a_sources/${name}`);
            await page.evaluate((value) => navigator.clipboard.writeText(value), `${measurementConfig.workspaceRoot}/a_sources/${name}`);
          }
          await prepareFileMutationMenu(page, "d000");
          const requestOffset = browserApiRequestRecords.length;
          const pageRequestOffset = pageRequests.get(page).length;
          const actualRequestOffset = actualMutationRequests.length;
          const readsBefore = await readCounters(page, "backend");
          const completionBaselines = await captureFileUiBaselines();
          const startTimeOriginMs = performance.timeOrigin;
          const start = performance.now();
          const startEpochMs = startTimeOriginMs + start;
          if (kind === "upload") {
            const chooser = page.waitForEvent("filechooser");
            await page.getByRole("menuitem", { name: "上传本地文件", exact: true }).click();
            await (await chooser).setFiles({ name, mimeType: "text/plain", buffer: Buffer.from("正式量化上传内容\n") });
          } else {
            await page.getByRole("menuitem", { name: /^粘贴\s+Ctrl\+V$/ }).click();
          }
          const fileSample = { start, start_time_origin_ms: startTimeOriginMs, start_now_ms: start,
            start_epoch_ms: startEpochMs, requestOffset, pageRequestOffset, actualRequestOffset, readsBefore, completionBaselines,
            name, kind, directory: "d000", page };
          const operation = await freezeFileOperation(fileSample);
          beginFileUiCompletion(operation, fileSample);
          timing = await finishFileMutation(page, operation, fileSample);
        }
        const { sample: mutationSample, facts } = timing;
        const fsWindow = await mutationSample.fsWindowPromise;
        const readsAfter = fsWindow.readsAfter;
        await settleResponseTasks();
        const attempts = summarizeFileMutationAttempts(facts, mutationSample);
        const acceptedAttempts = attempts.filter((attempt) => attempt.outcome === "accepted");
        if (!acceptedAttempts.length || acceptedAttempts.some((attempt) => !attempt.backend_fs_reads
          || attempt.receipt?.operation_id !== facts.operation_id
          || attempt.receipt?.client_sequence !== facts.client_sequence)) {
          throw new Error(`mutation attempt 原始数据不完整: ${JSON.stringify({ kind, pageId, facts, attempts })}`);
        }
        const firstAcceptedAttempt = acceptedAttempts[0];
        const windowReads = readsInWindow(mutationSample.readsBefore, readsAfter);
        const pageRelated = pageRequests.get(page).slice(mutationSample.pageRequestOffset);
        mutationSamples.push({ ...facts, expanded_directories: expanded,
          page_request_index_start: mutationSample.pageRequestOffset, page_request_index_end: pageRequests.get(page).length,
          http_requests: pageRelated.length,
          directory_reloads: pageRelated.filter((record) => record.method === "GET" && record.path === "/api/v1/workspace/files").length,
          backend_fs_reads: windowReads, backend_fs_read_count: sumFileSystemReadCounts(windowReads),
          backend_fs_window_before: mutationSample.readsBefore, backend_fs_window_after: readsAfter,
          fs_window: { endpoint_epoch_ms: fsWindow.endpoint_epoch_ms,
            deadline_epoch_ms: fsWindow.deadline_epoch_ms,
            timer_observed_epoch_ms: fsWindow.timer_observed_epoch_ms,
            observed_epoch_ms: fsWindow.observed_epoch_ms,
            length_ms: fsWindow.observed_epoch_ms - fsWindow.endpoint_epoch_ms },
          acceptance_request_fs_reads: firstAcceptedAttempt.backend_fs_reads,
          mutation_attempt_count: attempts.length, mutation_attempts: attempts,
          request_id: firstAcceptedAttempt.request_id });
        if (kind === "upload" && index === 0) {
          process.stdout.write(`file mutation ${expanded}: five real actions ready\n`);
          await openReadyMeasurementPage(page, expanded);
        }
        if (index === 19) process.stdout.write(`file mutation ${expanded}/${kind}: 20 samples complete\n`);
    }
    await observeUiStatus(page);
    await page.close();
  }
  let names = [];
  let missing = [];
  concurrentMissingFromUi = missing;
  let page;
  if (!minimalSamples) {
    const pages = await Promise.all([context.newPage(), context.newPage()]);
    if (focusConcurrent) await captureClockAlignment(pages[0], "run_before");
    for (const currentPage of pages) {
      attachMeasurementPageObservers(currentPage);
      await openReadyMeasurementPage(currentPage, 1);
      await ensureTreeDirectoryExpanded(currentPage, "concurrent");
    }
    for (let index = 0; index < 10; index += 1) {
      const pair = [`concurrent_a_${index}.txt`, `concurrent_b_${index}.txt`];
      names.push(...pair);
      concurrentExpectedNames.push(...pair);
      const outcomes = await Promise.allSettled(pages.map(async (currentPage, column) => {
        const { facts, sample } = await createViaMenu(currentPage, pair[column], "file", "concurrent");
        return { ...facts, mutation_attempts: summarizeFileMutationAttempts(facts, sample) };
      }));
      concurrentActions.push(...outcomes.flatMap((outcome) =>
        outcome.status === "fulfilled" ? [outcome.value] : []));
      const failedOutcome = outcomes.find((outcome) => outcome.status === "rejected");
      if (failedOutcome) throw failedOutcome.reason;
    }
    await Promise.all(pages.map((currentPage) => waitForMeasurementPageDelay(currentPage, 1000)));
    for (const [index, currentPage] of pages.entries()) {
      for (const name of names) {
        if (!(await findVisibleTreeRow(currentPage, `concurrent/${name}`))) missing.push({ page: index + 1, name });
      }
    }
    page = pages[0];
  } else {
    const pages = await Promise.all([context.newPage(), context.newPage()]);
    const attachedPageIds = pages.map((currentPage) => attachMeasurementPageObservers(currentPage));
    for (const currentPage of pages) {
      await openReadyMeasurementPage(currentPage, 1);
      await ensureTreeDirectoryExpanded(currentPage, "concurrent");
      await prepareFileMutationMenu(currentPage, "concurrent");
    }
    const pair = ["cross_page_a.txt", "cross_page_b.txt"];
    concurrentExpectedNames.push(...pair);
    const observationStart = { prepared: true, requestOffset: browserApiRequestRecords.length,
      actualRequestOffset: actualMutationRequests.length, completionBaselines: await captureFileUiBaselines(),
      confirmationPages: new Map(attachedPageIds.map((id, column) => [id, attachedPageIds[1 - column]])) };
    const queryHolds = [];
    // 最小代表组在发起页实际查询原ID时延后交付，等待另一页真实完成；
    // 共享outbox也可由另一页重派mutation直接消费终态，query hold只记录实际分支。
    // 不替换响应、不造业务事实，也不进入full量化组。
    await Promise.all(pages.map((currentPage, column) => currentPage.route("**/api/v1/workspace/files/operations?**", async (route) => {
      const ids = new URL(route.request().url()).searchParams.getAll("operation_id");
      const own = actualMutationRequests.slice(observationStart.actualRequestOffset).find((fact) =>
        fact.payload?.name === pair[column] && ids.includes(fact.payload.intent.client_operation_id));
      if (own && !queryHolds.some((hold) => hold.operation_id === own.payload.intent.client_operation_id)) {
        const operationId = own.payload.intent.client_operation_id;
        const peerPage = pages[1 - column];
        const peerBaseline = observationStart.completionBaselines.find((baseline) =>
          baseline.page_id === attachedPageIds[1 - column]);
        if (!peerBaseline) throw new Error("跨页 DOM 快照缺少操作前基线: " + operationId);
        const baseline = { completed_before: peerBaseline.completed_before,
          event_offset: peerBaseline.event_offset, snapshot_offset: peerBaseline.snapshot_offset };
        const targetPathTitle = path.resolve(measurementConfig.workspaceRoot, "concurrent", pair[column]);
        const deadline = performance.now() + 30000;
        let peerSnapshot = null;
        let acceptedQueueSeq = null;
        let terminalRevision = null;
        while (!peerSnapshot) {
          const acceptedPairs = browserApiRequestRecords.slice(observationStart.requestOffset).filter((record) =>
            record.status === 202 && record.browser_state === "response_completed"
            && record.receipt?.operation_id === operationId
            && record.receipt.client_sequence === own.payload.intent.client_sequence
            && Number.isSafeInteger(record.receipt.queue_seq) && record.receipt.queue_seq >= 1
            && record.proxy_exchange_id
            && actualMutationRequests.some((fact) => fact.browser_request_id === record.browser_request_id
              && fact.proxy_exchange_id === record.proxy_exchange_id
              && fact.payload?.intent?.client_operation_id === operationId));
          const acceptedQueues = new Set(acceptedPairs.map((record) => record.receipt.queue_seq));
          if (acceptedQueues.size > 1) throw new Error("跨页接受回执 queue_seq 漂移: " + operationId);
          const terminalReceipts = browserApiRequestRecords.slice(observationStart.requestOffset).flatMap((record) => [
            ...(record.receipt ? [record.receipt] : []), ...(record.operation_status?.operations ?? []),
          ]).filter((receipt) => receipt.operation_id === operationId
            && ["committed", "rejected", "cancelled", "dependency_failed"].includes(receipt.state));
          terminalReceipts.sort((left, right) => right.receipt_revision - left.receipt_revision);
          const terminal = terminalReceipts[0];
          if (terminal && (terminal.client_sequence !== own.payload.intent.client_sequence
            || !Number.isSafeInteger(terminal.queue_seq) || terminal.queue_seq < 1
            || !Number.isSafeInteger(terminal.receipt_revision) || terminal.receipt_revision < 1
            || acceptedQueues.size === 1 && terminal.queue_seq !== [...acceptedQueues][0])) {
            throw new Error("跨页终态与原operation接受身份/队列不一致: " + JSON.stringify(terminal));
          }
          if (terminal && (terminal.state !== "committed" || terminal.effect !== "complete")) {
            throw new Error("跨页快照等待时operation终态失败: " + JSON.stringify(terminal));
          }
          if (acceptedQueues.size === 1 && terminal?.state === "committed") {
            acceptedQueueSeq = acceptedPairs[0].receipt.queue_seq;
            terminalRevision = terminal.receipt_revision;
            peerSnapshot = await peerPage.evaluate(fileUiCompletionSnapshot, {
              operation: { operation_id: operationId,
                client_sequence: own.payload.intent.client_sequence,
                accepted_queue_seq: acceptedQueueSeq, receipt_revision: terminalRevision },
              baseline, target_path_title: targetPathTitle,
            });
          }
          if (peerSnapshot) break;
          if (performance.now() >= deadline) throw new Error("真实跨页terminal handoff超时: " + operationId);
          await waitForMeasurementPageDelay(currentPage, 20);
        }
        queryHolds.push({ operation_id: operationId, held_page: attachedPageIds[column],
          completion_page: attachedPageIds[1 - column], release_condition: "peer-frozen-operation-dom-snapshot",
          observation_source: peerSnapshot.observation_source, snapshot_index: peerSnapshot.snapshot_index,
          event_index: peerSnapshot.event_index, operation_id_from_snapshot: peerSnapshot.operation_id,
          client_sequence: peerSnapshot.client_sequence, queue_seq: peerSnapshot.queue_seq,
          receipt_revision: peerSnapshot.receipt_revision,
          baseline_snapshot_offset: peerBaseline.snapshot_offset, baseline_event_offset: peerBaseline.event_offset,
          target_path_title: peerSnapshot.target_path_title });
      }
      await route.continue();
    })));
    const outcomes = await Promise.all(pages.map((currentPage, column) =>
      createViaMenu(currentPage, pair[column], "file", "concurrent", observationStart)));
    concurrentActions = outcomes.map(({ facts, sample }) => ({
      ...facts,
      mutation_attempts: summarizeFileMutationAttempts(facts, sample),
    }));
    concurrentExpectedNames = pair;
    const handoffs = outcomes.map(({ facts }) => ({ operation_id: facts.operation_id,
      initiating_page: facts.initiating_page, completion_page: facts.confirmation_page,
      response_url: facts.ui_completion.event.terminal.response_url,
      receipt_ordinal: facts.ui_completion.event.terminal.ordinal,
      completion_ordinal: facts.ui_completion.event.ordinal,
      source: "actual-consumed-terminal-and-ui-completed" }));
    const identityGuards = [];
    for (const { facts, sample } of outcomes) {
      const confirmationPage = pages.find((currentPage) => pageIds.get(currentPage) === facts.confirmation_page);
      const baseline = sample.completionBaselines.find((value) => value.page_id === facts.confirmation_page);
      const state = { operation: facts,
        baseline: { completed_before: baseline.completed_before, event_offset: baseline.event_offset,
          snapshot_offset: baseline.snapshot_offset },
        candidate_snapshot: facts.ui_completion, candidate_event: facts.ui_completion.event,
        candidate_event_index: facts.ui_completion.event_index,
        target_path_title: facts.ui_completion.target_path_title };
      const other = outcomes.find((value) => value.facts.operation_id !== facts.operation_id).facts;
      const wrongOperation = await confirmationPage.evaluate(fileUiCompletionSnapshot, {
        ...state, operation: { ...facts, operation_id: other.operation_id },
      });
      const outOfOrder = await confirmationPage.evaluate(fileUiCompletionSnapshot, {
        ...state, candidate_event: { ...state.candidate_event, ordinal: state.candidate_event.terminal.ordinal },
      });
      if (wrongOperation || outOfOrder) throw new Error("错operation或未先消费终态的UI事件通过了完成等待器");
      identityGuards.push({ operation_id: facts.operation_id, rejected_wrong_operation_id: other.operation_id,
        observation_source: state.candidate_snapshot.observation_source,
        snapshot_index: state.candidate_snapshot.snapshot_index,
        event_index: state.candidate_snapshot.event_index,
        client_sequence: facts.client_sequence, queue_seq: facts.accepted_queue_seq,
        receipt_revision: facts.receipt_revision,
        baseline_snapshot_offset: baseline.snapshot_offset, baseline_event_offset: baseline.event_offset,
        wrong_operation_accepted: Boolean(wrongOperation), out_of_order_accepted: Boolean(outOfOrder) });
    }
    await settleResponseTasks();
    names = pair;
    crossPageObservation = { samples: outcomes.map(({ facts, sample }) => ({ ...facts,
      mutation_attempts: summarizeFileMutationAttempts(facts, sample),
    })), distinct_operation_ids: new Set(outcomes.map(({ facts }) => facts.operation_id)).size,
    cross_page_dispatch_count: outcomes.filter(({ facts }) => facts.cross_page_dispatched).length,
    cross_page_confirmation_count: outcomes.filter(({ facts }) => facts.cross_page_confirmed).length,
    terminal_handoffs: handoffs, terminal_query_holds: queryHolds,
    completion_identity_guards: identityGuards, attached_page_ids: attachedPageIds };
    await Promise.all(pages.map(async (currentPage) => {
      await observeUiStatus(currentPage);
      await currentPage.close();
    }));
    page = await context.newPage();
    attachMeasurementPageObservers(page);
    await openReadyMeasurementPage(page, 1);
  }
  for (const kind of (focusConcurrent ? [] : ["create_folder", "move_workspace"])) {
    for (let index = 0; index < (minimalSamples ? 1 : 20); index += 1) {
      const name = `gateway_measure_${index}`;
      const parent = measurementConfig.gatewayFolderIds[index % measurementConfig.gatewayFolderIds.length];
      if (kind === "create_folder") await page.locator("#gateway-name").fill(name);
      else await page.locator("#gateway-parent").selectOption(parent);
      const requestOffset = pageRequests.get(page).length;
      const actualRequestOffset = actualMutationRequests.length;
      const readsBefore = await readCounters(page, "gateway");
      const start = performance.now();
      await page.locator(kind === "create_folder" ? "#gateway-create" : "#gateway-move").click();
      await page.waitForFunction((value) => document.getElementById("gateway-status")?.textContent === value,
        kind === "create_folder" ? `已创建:${name}` : `已移动:${parent}`);
      const elapsed = performance.now() - start;
      await waitForMeasurementPageDelay(page, 100);
      const readsAfter = await readCounters(page, "gateway");
      await settleResponseTasks();
      const related = pageRequests.get(page).slice(requestOffset);
      const write = related.find((record) => isActualMutationRequest(record.method, record.path)
        && record.path.startsWith("/api/gateway/workspace-navigation/"));
      if (!write?.gateway_reads || write.status !== 200 || write.browser_state !== "response_completed") {
        throw new Error(`Gateway 自有 mutation 数据不完整: ${JSON.stringify(related)}`);
      }
      const actualRequests = actualMutationRequests.slice(actualRequestOffset)
        .filter((fact) => isActualMutationRequest(fact.method, fact.path) && fact.path === write.path);
      const requestFact = actualRequests[0];
      if (actualRequests.length !== 1 || !requestFact || requestFact.url !== write.request?.url()) {
        throw new Error(`Gateway 实际 mutation 请求事实无法唯一配对: ${JSON.stringify({ path: write.path,
          actual_request_count: actualRequests.length, payload_errors: actualRequests.map((fact) => fact.payload_error).filter(Boolean) })}`);
      }
      write.actual_request = actualMutationRequestSummary(requestFact);
      if (requestFact.payload_error) throw new Error(`Gateway 实际 mutation 请求正文解析失败: ${requestFact.payload_error}`);
      let uiNode = null;
      let returnedNode = null;
      if (minimalSamples) {
        const payload = requestFact.payload;
        const expectedParent = kind === "create_folder" ? null : parent;
        returnedNode = write.navigation_nodes?.find((node) => kind === "create_folder"
          ? node.kind === "workspace_folder" && node.name === name
          : node.kind === "workspace_ref" && node.node_id === measurementConfig.workspaceNodeId);
        if (!requestFact.has_local_token
          || (kind === "create_folder" && (write.path !== "/api/gateway/workspace-navigation/folders"
            || payload.name !== name || payload.parent_node_id !== null))
          || (kind === "move_workspace" && (write.path !== "/api/gateway/workspace-navigation/placement"
            || payload.node_id !== measurementConfig.workspaceNodeId || payload.parent_node_id !== parent || payload.mode !== "last"))
          || !returnedNode || returnedNode.parent_node_id !== (kind === "create_folder" ? null : parent)
          || (kind === "move_workspace" && returnedNode.workspace_id !== measurementConfig.workspaceId)) {
          throw new Error(`Gateway mutation 请求或返回节点目标不一致: ${JSON.stringify({ kind, write, payload, returnedNode })}`);
        }
        uiNode = await page.locator("#gateway-navigation li").evaluateAll((nodes, identity) =>
          nodes.filter((node) => node.getAttribute("data-node-id") === identity.nodeId)
            .map((node) => ({ name: node.textContent, kind: node.getAttribute("data-kind"),
              parent: node.getAttribute("data-parent"), workspaceId: node.getAttribute("data-workspace-id") })),
          { nodeId: returnedNode.node_id });
        if (uiNode.length !== 1 || uiNode[0].parent !== (expectedParent ?? "root")
          || uiNode[0].kind !== (kind === "create_folder" ? "workspace_folder" : "workspace_ref")
          || (kind === "create_folder" && uiNode[0].name !== name)
          || (kind === "move_workspace" && uiNode[0].workspaceId !== measurementConfig.workspaceId)) {
          throw new Error(`Gateway UI 未显示原操作目标节点: ${JSON.stringify({ kind, uiNode, returnedNode })}`);
        }
      }
      const windowReads = readsInWindow(readsBefore, readsAfter);
      gatewaySamples.push({ kind, name, parent, elapsed_ms: elapsed, http_requests: related.length,
        gateway_fs_reads: windowReads, gateway_fs_read_count: sumFileSystemReadCounts(windowReads),
        gateway_fs_window_before: readsBefore, gateway_fs_window_after: readsAfter,
        mutation_request_fs_reads: write.gateway_reads, request_id: write.request_id,
        ...(minimalSamples ? { request_has_local_token: requestFact.has_local_token,
          request_workspace_id: requestFact.workspace_id_header, request_payload: requestFact.payload_summary,
          returned_navigation_node: returnedNode, ui_node: uiNode } : {}) });
    }
  }
  const groups = [];
  for (const expanded of (focusConcurrent ? [] : minimalSamples ? [1] : [1, 20, 200])) for (const kind of ["file", "directory", "paste", "copy", "upload"]) {
    const samples = mutationSamples.filter((item) => item.expanded_directories === expanded && item.kind === kind);
    groups.push({ expanded_directories: expanded, kind, sample_count: samples.length,
      ...(samples.length ? { p95_ms: calculateP95(samples.map((item) => item.elapsed_ms)),
        reloads: samples.map((item) => item.directory_reloads), fs_reads: samples.map((item) => item.backend_fs_read_count) } : {}) });
  }
  const maxP95 = minimalSamples || focusConcurrent ? null : Math.max(...groups.map((group) => group.p95_ms));
  const maxReloads = minimalSamples || focusConcurrent ? null : Math.max(...mutationSamples.map((sample) => sample.directory_reloads));
  const reloadGrowth = !minimalSamples && !focusConcurrent && ["file", "directory", "paste", "copy", "upload"].some((kind) => {
    const counts = [1, 20, 200].map((expanded) => {
      const values = groups.find((group) => group.kind === kind && group.expanded_directories === expanded).reloads;
      return [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
    });
    return counts[0] <= counts[1] && counts[1] <= counts[2] && counts[0] < counts[2];
  });
  await settleResponseTasks();
  if (traceConcurrent) {
    backendConcurrentObservation = await saveBackendConcurrentObservation();
    validateBackendConcurrentObservation(backendConcurrentObservation);
  }
  concurrentActions.sort((left, right) => names.indexOf(left.name) - names.indexOf(right.name));
  return { evidence_level: "Integration", scope: "真实文件树组件与 Gateway 自有导航 hook、真实 Gateway/Workspace HTTP；文件操作等待原 operation committed/effect complete、物理发布和 UI 完成；FS 计数使用进程累计窗口覆盖异步 worker、轮询与目录同步，单请求 header 仅作诊断",
    browser: await browser.version(), clock_alignment: { method: "performance.timeOrigin + performance.now() at run boundaries; browser/node brackets retained",
      samples: clockAlignmentSamples, errors: clockAlignmentErrors }, groups, mutation_samples: mutationSamples,
    gateway_samples: gatewaySamples, gateway_summary: ["create_folder", "move_workspace"].map((kind) => ({
      kind, p95_ms: calculateP95(gatewaySamples.filter((sample) => sample.kind === kind).map((sample) => sample.elapsed_ms)),
      request_counts: gatewaySamples.filter((sample) => sample.kind === kind).map((sample) => sample.http_requests),
      fs_read_counts: gatewaySamples.filter((sample) => sample.kind === kind).map((sample) => sample.gateway_fs_read_count),
    })),
    concurrent: { expected_names: names, missing_from_ui: missing, actions: concurrentActions },
    backend_concurrent_observation: backendConcurrentObservation,
    cross_page_observation: crossPageObservation,
    file_gate: minimalSamples ? { decision: "not_evaluated", reason: "最小样本仅验证消费者与目标归属，不参与 10.4b 门控" }
      : focusConcurrent ? { decision: "not_evaluated", reason: "并发阶段诊断不参与 10.4b 性能门控" }
      : { max_p95_ms: maxP95, max_reloads: maxReloads, reload_growth: reloadGrowth,
        decision: maxP95 > 1000 || reloadGrowth || missing.length ? "10.4b-1" : maxP95 < 300 && maxReloads <= 2 ? "10.4b-2" : "10.4b-3" },
    sse_connection_requests: browserApiRequestRecords.filter((request) => request.path.endsWith("/events")).length,
    requests: browserApiRequestRecords, actual_mutation_requests: actualMutationRequests.map(actualMutationRequestSummary),
    operation_exchange_index: operationExchangeIndex(),
    observation_requests: observationRequests, probe_traces: probeTraces, ui_status_observations: uiStatusObservations,
    file_ui_failure_diagnostics: fileUiFailureDiagnostics,
    cross_page_observation: crossPageObservation,
    measurement_window: "导航准备与各页完成基线在start前；原发起页locator与冻结operation终态消费/对应UI完成/双rAF终点；30s完成期限和650ms FS窗口保留；额外视口核验在elapsed与FS窗口后",
    browser_errors: failures };
}
let result;
let measurementError;
const cleanupErrors = [];
async function cleanup(label, action) {
  try {
    await action();
  } catch (error) {
    cleanupErrors.push({ stage: label, error_type: error.name, error: error.message, stack: error.stack });
    measurementError ??= error;
  }
}
try {
  if (observationOnly) {
    const page = await context.newPage();
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const exposed = await page.evaluate(() => Object.hasOwn(window.measurementConfig, "observationHeaders"));
    if (exposed) throw new Error("观察凭据不得进入浏览器组件配置");
    for (const runtime of ["backend", "gateway"]) await readCounters(page, runtime);
    await writeFile(path.join(artifacts, "observation-requests.json"), JSON.stringify({
      evidence_level: "Integration", observation_requests: observationRequests, probe_traces: probeTraces,
      observation_headers_exposed_to_browser: exposed,
    }, null, 2) + "\n");
    await page.close();
  } else {
    result = await measureMutations();
  }
} catch (error) {
  measurementError = error;
} finally {
  if (!observationOnly && !clockAlignmentSamples.some((sample) => sample.label === "run_after")) {
    const clockPage = context.pages().find((page) => pageIds.has(page) && !page.isClosed());
    if (clockPage) {
      try {
        await captureClockAlignment(clockPage, "run_after");
      } catch (error) {
        // 失败详情已记录在 clockAlignmentErrors，原始测量失败继续向外报告。
        if (measurementError) cleanupErrors.push({ stage: "run_after clock alignment", error_type: error.name,
          error: error.message, stack: error.stack });
        else measurementError = error;
      }
    } else {
      clockAlignmentErrors.push({ label: "run_after", error: "没有可采样的已连接测量页" });
    }
  }
  await cleanup("UI 状态观察", () => Promise.all(
    context.pages().filter((page) => pageIds.has(page)).map(observeUiStatus),
  ));
  if (traceConcurrent && !observationOnly && !backendConcurrentObservationAttempted) {
    try {
      backendConcurrentObservation = await saveBackendConcurrentObservation();
      validateBackendConcurrentObservation(backendConcurrentObservation);
    } catch (error) {
      backendConcurrentObservationError = error.message;
      if (!measurementError) measurementError = error;
      else process.stderr.write(`backend concurrent trace 导出失败，保留原测量错误: ${error.stack ?? error.message}\n`);
    }
  }
  await cleanup("Playwright context close", () => context.close());
  await cleanup("browser close", () => browser.close());
  await cleanup("proxy response task settlement", () => settleResponseTasks());
  await cleanup("proxy response destruction", async () => {
    for (const incoming of proxyResponses) incoming.destroy();
  });
  await cleanup("proxy server close", () => new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
    server.closeAllConnections();
  }));
  await cleanup("raw exchange flush", () => flushRawExchanges());
}
failures.push(...browserApiRequestRecords.filter((record) => record.response_error && record.network_state === "response_completed"
  && !actualMutationRequests.some((fact) => fact.browser_request_id === record.browser_request_id && fact.proxy_error))
  .map((record) => `响应观察失败: ${record.path}: ${record.response_error}`));
measurementError ??= failures.length ? new Error(`浏览器错误: ${failures.join("；")}`) : null;
if (measurementError) {
  if (backendConcurrentObservationError) {
    process.stderr.write(`backend concurrent trace 诊断错误: ${backendConcurrentObservationError}\n`);
  }
  await writeFile(path.join(artifacts, "failed-measurements.json"), JSON.stringify({ message: measurementError.message,
    error_name: measurementError.name, error_stack: measurementError.stack,
    clock_alignment: { method: "performance.timeOrigin + performance.now() at run boundaries; browser/node brackets retained",
      samples: clockAlignmentSamples, errors: clockAlignmentErrors }, mutationSamples, gatewaySamples,
    requests: browserApiRequestRecords, actual_mutation_requests: actualMutationRequests.map(actualMutationRequestSummary), observationRequests, probe_traces: probeTraces,
    operation_exchange_index: operationExchangeIndex(),
    concurrent: { expected_names: concurrentExpectedNames, missing_from_ui: concurrentMissingFromUi,
      actions: concurrentActions },
    backend_concurrent_observation: backendConcurrentObservation,
    backend_concurrent_observation_error: backendConcurrentObservationError,
    raw_exchange_index: "raw-exchanges/index.json",
    cleanup_errors: cleanupErrors, ui_status_observations: uiStatusObservations,
    file_ui_failure_diagnostics: fileUiFailureDiagnostics, failures }, null, 2) + "\n");
  throw measurementError;
}
if (result) {
  result.actual_mutation_requests = actualMutationRequests.map(actualMutationRequestSummary);
  await writeFile(path.join(artifacts, "measurements.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify({ file_gate: result.file_gate, gateway_summary: result.gateway_summary,
    concurrent: result.concurrent, sample_count: mutationSamples.length, minimal_samples: minimalSamples }) + "\n");
}
