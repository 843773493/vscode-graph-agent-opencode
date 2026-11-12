import { afterEach, expect, test } from "bun:test";
import { discardConfigPending, getConfigSourceDocument, listConfigSources, preflightConfigSource, retryConfigPending, saveConfigSource } from "./configSources";
import { HttpRequestError } from "./http";

const originalFetch = globalThis.fetch;
const target = { gateway_id: "gateway_00000000000040008000000000000001", workspace_id: "00000000-0000-4000-8000-000000000001" };
const sourceKey = "workspace_root_mutable_override";
let port = 59_580;
afterEach(() => { globalThis.fetch = originalFetch; });

function respond(document: Record<string, unknown> | ((url: URL, init?: RequestInit) => Record<string, unknown> | Response),
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
  const document = { ...target, source_key: sourceKey, jsonc: "// 原正文\r\n{}\r\n", file_digest: "file", layer: "workspace",
    writable: true, file_presence: "present", source_generation: "generation", json_schema: {}, effective: {}, source_effective: {},
    layers: [], origins: [], status: undefined, layer_revision: 3, layer_digest: "layer", active_revision: 2, active_digest: "active" };
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

test("来源列表、预检和保存沿同一目标传输完整意图；预检只调用无副作用端点", async () => {
  const requests: { path: string; method: string; body: unknown }[] = [];
  const document = { ...target, source_key: sourceKey, jsonc: intent.jsonc, layer: "workspace", writable: true, file_presence: "present",
    file_digest: "file", layer_revision: 7, layer_digest: "layer", source_generation: 9, active_revision: 4, active_digest: "active",
    json_schema: {}, effective: {}, source_effective: {}, layers: [], origins: [], status: undefined };
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
