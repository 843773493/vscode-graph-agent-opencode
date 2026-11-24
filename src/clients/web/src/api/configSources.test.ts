import { afterEach, expect, test } from "bun:test";
import { discardConfigPending, discoverLlmModels, getConfigSourceDocument, listConfigSources, preflightConfigSource, retryConfigPending, saveConfigSource, testLlmModel } from "./configSources";
import { HttpRequestError } from "./http";
import { ConfigModelInspectionDTO, ConfigProviderInspectionDTO, ConfigSourceDocumentDTO, LlmDiscoverResultDTO, type LlmTestRequest } from "../types/protocol_generated/boxteam/workspace/v2/public";

const originalFetch = globalThis.fetch;
const target = { gateway_id: "gateway_00000000000040008000000000000001", workspace_id: "00000000-0000-4000-8000-000000000001" };
const sourceKey = "workspace_root_mutable_override";
let port = 59_580;
afterEach(() => { globalThis.fetch = originalFetch; });

const modelTest: LlmTestRequest = { ...target, mode: "draft", provider_id: "team", model_id: "main", edit_version: 2,
  source_key: sourceKey, jsonc: "{}", expected_source_baseline: {}, timeout_seconds: 60, streaming: true };
const observation = { ...target, mode: "draft", provider_id: "team", edit_version: 2, source_key: sourceKey,
  source_baseline: {}, credential: { source: "none" },
  protocol: "chat_completions", endpoint: "https://model.test/v1", observed_at: "2026-11-17T00:00:00Z" };
const start = { request_id: "model-test", observation, model_ref: { provider_id: "team", model_id: "main" }, streaming: true };
const delta = { request_id: "model-test", text: "Hello" };
const result = { ...start, status: "passed", first_content_ms: 5, elapsed_ms: 8, preview: "Hello",
  preview_truncated: false, usage: null, error: null };
const frame = (event: string, value: object) => new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(value)}\n\n`);
const sseResponse = (body: BodyInit) => new Response(body, { headers: { "Content-Type": "text/event-stream", "X-Request-ID": "model-test" } });

test("模型测试单次POST先展示start和增量，再消费冻结观察的终态", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  let count = 0;
  const current = respond((_url, init) => {
    count++;
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual(modelTest);
    return sseResponse(stream);
  });
  let opened!: () => void;
  const ready = new Promise<void>(resolve => { opened = resolve; });
  const content: string[] = [];
  const pending = testLlmModel(current, target, modelTest, { signal: new AbortController().signal,
    onStart: event => { expect(event).toEqual(start); opened(); }, onDelta: event => content.push(event.text) });
  controller.enqueue(frame("start", start));
  await ready;
  controller.enqueue(frame("delta", delta));
  // 合法 JSON 的键顺序变化不能被误判为另一个观察版本。
  controller.enqueue(frame("result", { ...result, observation: Object.fromEntries(Object.entries(observation).reverse()) }));
  controller.close();
  expect((await pending).status).toBe("passed");
  expect(content).toEqual(["Hello"]);
  expect(count).toBe(1);
});

test.each(["missing-result", "duplicate-start", "wrong-request", "wrong-owner", "wrong-model", "changed-observation", "after-terminal", "wrong-shape"])(
  "模型测试拒绝不完整或串线事件，不重发POST：%s", async failure => {
    const parts = [frame("start", start)];
    if (failure === "duplicate-start") parts.push(frame("start", start));
    if (failure === "wrong-shape") parts.push(frame("delta", { request_id: "model-test", text: 1 }));
    if (failure !== "missing-result") parts.push(frame("result", failure === "wrong-request" ? { ...result, request_id: "another" }
      : failure === "wrong-owner" ? { ...result, observation: { ...observation, gateway_id: "another" } }
      : failure === "wrong-model" ? { ...result, model_ref: { ...result.model_ref, model_id: "another" } }
      : failure === "changed-observation" ? { ...result, observation: { ...observation, credential: { source: "vault", revision: 9 } } } : result));
    if (failure === "after-terminal") parts.push(frame("delta", delta));
    let count = 0;
    const current = respond(() => { count++; return sseResponse(new Blob(parts)); });
    await expect(testLlmModel(current, target, modelTest, { signal: new AbortController().signal,
      onStart: () => {}, onDelta: () => {} })).rejects.toThrow();
    expect(count).toBe(1);
  },
);

test("模型测试首内容前取消回收正文reader并保留用户取消原因", async () => {
  let closed = false, count = 0;
  const abort = new AbortController();
  const cancelled = new Error("用户停止本次测试");
  const current = respond(() => {
    count++;
    return sseResponse(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(frame("start", start)); }, cancel() { closed = true; },
    }));
  });
  await expect(testLlmModel(current, target, modelTest, { signal: abort.signal,
    onStart: () => abort.abort(cancelled), onDelta: () => { throw new Error("取消后不能接收内容"); } })).rejects.toBe(cancelled);
  expect(closed).toBe(true);
  expect(count).toBe(1);
});

test("模型发现沿原目标发送单次草稿；部分目录及观察版本保持原回执", async () => {
  const input = { ...target, mode: "draft", provider_id: "team", edit_version: 3, source_key: sourceKey,
    jsonc: "// 未保存草稿\n{}", expected_source_baseline: {} };
  const result = LlmDiscoverResultDTO.fromPartial({ observation: { gateway_id: input.gateway_id, workspace_id: input.workspace_id,
    mode: input.mode, provider_id: input.provider_id, edit_version: input.edit_version, source_key: input.source_key,
    source_baseline: {}, protocol: "responses", endpoint: "https://model.test/v1",
    credential: { source: "temporary" }, observed_at: "2026-11-17T00:00:00Z" }, source: "upstream", models: [{ model: "model-a" }],
    complete: false, error: { category: "pagination", message: "当前目录不完整" } });
  let count = 0;
  const route = { ...target, gateway_connection_id: "original-connection" };
  const current = respond(result, (url, init) => {
    count++;
    expect(url.pathname).toBe("/api/v1/config/llm/discover");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual(input);
    expect(new Headers(init?.headers).get("X-BoxTeam-Gateway-Connection-Id")).toBe(route.gateway_connection_id);
  });
  expect(await discoverLlmModels(current, route, input)).toEqual(result);
  expect(count).toBe(1);
});

test.each(["gateway_id", "workspace_id", "provider_id", "mode", "edit_version", "source_key"])("模型发现拒绝迟到的另一观察：%s", async field => {
  const input = { ...target, mode: "draft", provider_id: "team", edit_version: 3, source_key: sourceKey,
    jsonc: "{}", expected_source_baseline: {} };
  const current = respond({ observation: { ...input, [field]: "changed" }, models: [] });
  await expect(discoverLlmModels(current, target, input)).rejects.toThrow("编辑版本");
});

test("active模型目录接受HTTP空来源，取消不重发模型发现", async () => {
  const input = { ...target, mode: "active", provider_id: "team", edit_version: 0, expected_source_baseline: {},
    expected_active_revision: 1, expected_active_digest: "active" };
  let count = 0;
  const current = respond({ observation: { ...input, source_key: null }, models: [] }, () => { count++; });
  await discoverLlmModels(current, target, input);
  const controller = new AbortController();
  const cancelled = new Error("用户取消目录发现");
  controller.abort(cancelled);
  await expect(discoverLlmModels(current, target, input, controller.signal)).rejects.toBe(cancelled);
  expect(count).toBe(1);
});

function respond(document: object | ((url: URL, init?: RequestInit) => object | Response),
  inspect: (url: URL, init?: RequestInit) => void = () => {}) {
  globalThis.fetch = Object.assign(async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.pathname === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "config-test-token" } });
    inspect(url, init);
    const result = typeof document === "function" ? document(url, init) : document;
    return result instanceof Response ? result : Response.json({ code: 0, message: "ok", request_id: "source-read", data: result });
  }, { preconnect: originalFetch.preconnect });
  return port++;
}

test("来源读取保留原正文与全部CAS，查询原提交携带同一幂等键和完整owner", async () => {
  const document = ConfigSourceDocumentDTO.fromPartial({ ...target, source_key: sourceKey, jsonc: "// 原正文\r\n{}\r\n", file_digest: "file", layer: "workspace",
    writable: true, file_presence: "present", source_generation: "generation", json_schema: {}, effective: {}, source_effective: {},
    layers: [], origins: [], source_baseline: {}, active_origins: [], source_inherited: {}, nodes: [], status: undefined,
    layer_revision: 3, layer_digest: "layer", active_revision: 2, active_digest: "active" });
  const seen: URL[] = [];
  const controller = new AbortController();
  const route = { ...target, gateway_connection_id: 'original-connection' };
  const current = respond(document, (url, init) => {
    seen.push(url);
    expect(init?.cache).toBe("no-store");
    expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    expect(new Headers(init?.headers).get('X-BoxTeam-Gateway-Connection-Id')).toBe(route.gateway_connection_id);
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });
  expect(await getConfigSourceDocument(current, route, sourceKey, { signal: controller.signal })).toEqual(document);
  const key = "original+/=提交";
  expect(await getConfigSourceDocument(current, route, sourceKey, { idempotencyKey: key, signal: controller.signal })).toEqual(document);
  expect(seen.map(url => url.pathname)).toEqual([`/api/v1/config/sources/${sourceKey}`, `/api/v1/config/sources/${sourceKey}`]);
  expect(seen[0]!.search).toBe("");
  expect(seen[1]!.searchParams.get("idempotency_key")).toBe(key);
});

const intent = { ...target, source_key: sourceKey, jsonc: '// 保留注释\n{"default_agent":"reviewer"}\n', base_file_digest: "file-old",
  base_layer_revision: 7, base_layer_digest: "layer-old", expected_active_revision: 4, expected_active_digest: "active-old", idempotency_key: "fixed-intent" };

test("选定模型预检只增加查询引用，沿原来源意图取回同一模型的后端投影", async () => {
  const modelRef = { provider_id: "team+/", model_id: "main model" };
  const inspection = ConfigModelInspectionDTO.fromPartial({ model_ref: modelRef, model: "upstream-main", options: { temperature: 0 }, origins: [],
    request_url: "https://model.test/v1/responses", config_ref: "#/profiles/model", node_id: "model", chain: [],
    local_settings: {}, inherited_settings: {}, network: {}, shared: false });
  const providerInspection = ConfigProviderInspectionDTO.fromPartial({ provider_id: modelRef.provider_id });
  let count = 0;
  const current = respond((url, init) => {
    count++;
    expect(url.pathname).toBe(`/api/v1/config/sources/${sourceKey}/preflight`);
    expect(url.searchParams.get("provider_id")).toBe(modelRef.provider_id);
    expect(url.searchParams.get("model_id")).toBe(modelRef.model_id);
    expect(JSON.parse(String(init?.body))).toEqual(intent);
    return { document: { ...target, source_key: sourceKey }, provider_options: providerInspection, model_options: inspection };
  });
  expect((await preflightConfigSource(current, target, intent, undefined, modelRef)).model_options).toEqual(inspection);
  expect(count).toBe(1);
});

test.each([undefined, { provider_id: "team", model_id: "another" }])("选定模型预检拒绝缺失或串线的模型回执 %j", async model_ref => {
  const current = respond({ document: { ...target, source_key: sourceKey }, model_options: model_ref ? { model_ref } : undefined });
  await expect(preflightConfigSource(current, target, intent, undefined, { provider_id: "team", model_id: "main" })).rejects.toThrow();
});

test("来源列表、预检和保存沿同一目标传输完整意图；预检只调用无副作用端点", async () => {
  const requests: { path: string; method: string; body: unknown }[] = [];
  const document = ConfigSourceDocumentDTO.fromPartial({ ...target, source_key: sourceKey, jsonc: intent.jsonc, layer: "workspace", writable: true, file_presence: "present",
    file_digest: "file", layer_revision: 7, layer_digest: "layer", source_generation: 9, active_revision: 4, active_digest: "active",
    json_schema: {}, effective: {}, source_effective: {}, layers: [], origins: [], source_baseline: {}, active_origins: [], source_inherited: {},
    nodes: [], status: undefined });
  const saved = { ...document, edit: { idempotency_key: intent.idempotency_key, candidate_id: "candidate", state: "active",
    file_digest: "file-saved", source_saved: true, layer_imported: true } };
  const current = respond((url, init) => {
    requests.push({ path: url.pathname, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
    expect(init?.cache).toBe("no-store");
    expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    if (url.pathname.endsWith("/preflight")) return { document, candidate: { default_agent: "reviewer" }, changed_paths: ["/default_agent"] };
    if (url.pathname.endsWith("/sources")) return { sources: [{ source_key: sourceKey, uri: null, layer: "workspace" }] };
    return saved;
  });
  expect((await listConfigSources(current, target)).sources[0]!.source_key).toBe(sourceKey);
  expect((await preflightConfigSource(current, target, intent)).document).toEqual(document);
  expect(requests.map(item => item.method)).toEqual(["GET", "POST"]);
  expect(await saveConfigSource(current, target, intent)).toEqual(saved);
  expect(requests).toEqual([
    { path: "/api/v1/config/sources", method: "GET", body: undefined },
    { path: `/api/v1/config/sources/${sourceKey}/preflight`, method: "POST", body: intent },
    { path: `/api/v1/config/sources/${sourceKey}`, method: "PUT", body: intent },
  ]);
});

test("异目标意图在传输前拒绝，预检与保存都不写其它工作区", async () => {
  let count = 0;
  const current = respond({}, () => { count++; });
  await expect(preflightConfigSource(current, target, { ...intent, gateway_id: "other" })).rejects.toThrow("编辑意图");
  await expect(saveConfigSource(current, target, { ...intent, workspace_id: "other" })).rejects.toThrow("编辑意图");
  expect(count).toBe(0);
});

test.each([{ gateway_id: "other" }, { workspace_id: "other" }, { source_key: "other" }])("预检与保存拒绝异目标回执 %j", async change => {
  const current = respond(url => {
    const document = { ...target, source_key: sourceKey, ...change, edit: { idempotency_key: intent.idempotency_key } };
    return url.pathname.endsWith("/preflight") ? { document } : document;
  });
  await expect(preflightConfigSource(current, target, intent)).rejects.toThrow("原 Gateway／工作区");
  await expect(saveConfigSource(current, target, intent)).rejects.toThrow("原 Gateway／工作区");
});

test("保存拒绝另一幂等提交或缺少提交的回执，预检要求完整来源文档", async () => {
  let current = respond({ ...target, source_key: sourceKey, edit: { idempotency_key: "another" } });
  await expect(saveConfigSource(current, target, intent)).rejects.toThrow("原 Gateway／工作区");
  current = respond({ ...target, source_key: sourceKey });
  await expect(saveConfigSource(current, target, intent)).rejects.toThrow("没有原提交记录");
  await expect(preflightConfigSource(current, target, intent)).rejects.toThrow("完整文档");
});

test("来源冲突保留原HTTP错误和request_id，客户端零自动保存重放", async () => {
  let count = 0;
  const current = respond(() => {
    count++;
    return Response.json({ detail: "原 active CAS 已改变", request_id: "source-conflict" }, { status: 409, headers: { "X-Request-ID": "source-conflict" } });
  });
  let error: unknown;
  try { await saveConfigSource(current, target, intent); } catch (cause) { error = cause; }
  expect(error).toBeInstanceOf(HttpRequestError);
  expect(error).toMatchObject({ status: 409, requestId: "source-conflict" });
  expect(count).toBe(1);
});

test("pending操作精确携带candidate_ref与active CAS，不调用重启或resolve", async () => {
  const requests: { path: string; candidate: string | null; method: string | undefined; body: unknown }[] = [];
  const current = respond((url, init) => {
    requests.push({ path: url.pathname, candidate: url.searchParams.get("candidate_ref"), method: init?.method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined });
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    return { state: "pending_restart", active_revision: 4, pending_revision: 5, restart_required: true };
  });
  const ref = "original+/=候选";
  expect((await retryConfigPending(current, target, ref)).state).toBe("pending_restart");
  await discardConfigPending(current, target, ref, { active_revision: 4, active_digest: "original-active" });
  expect(requests).toEqual([
    { path: "/api/v1/config/pending/retry", candidate: ref, method: "POST", body: undefined },
    { path: "/api/v1/config/pending/discard", candidate: ref, method: "POST", body: { expected_active_revision: 4, expected_active_digest: "original-active" } },
  ]);
});

test.each([
  { gateway_id: "other" }, { workspace_id: "other" }, { source_key: "other" }, { edit: { idempotency_key: "other" } },
])("拒绝异主、异来源或另一提交的文档 %j", async change => {
  const current = respond({ ...target, source_key: sourceKey, ...change });
  await expect(getConfigSourceDocument(current, target, sourceKey, { idempotencyKey: "original" })).rejects.toThrow("原 Gateway／工作区");
});

test("迟到读取仍按发起时owner核对，原信号中止拒绝消费结果", async () => {
  const route = { ...target };
  const controller = new AbortController();
  const reason = new Error("原身份失效");
  const current = respond({ ...target, source_key: sourceKey }, (_url, init) => {
    route.workspace_id = "new-focus";
    controller.abort(reason);
    expect(init?.signal?.aborted).toBe(true);
  });
  await expect(getConfigSourceDocument(current, route, sourceKey, { signal: controller.signal })).rejects.toBe(reason);
});
