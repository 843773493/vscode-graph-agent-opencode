import { afterEach, expect, test } from "bun:test";
import { FederatedSearchRequest } from "../../types/protocol_generated/boxteam/search/v1/public";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "../session/sessionApiFetchMock";
import { IncompleteSearchStreamError, streamFederatedSearch } from "./federatedSearch";

const owners = ["A", "B"].map(workspace_id => ({ gateway_id: "gateway", workspace_id }));
const scope = { version: 1, gateway_id: "gateway", resolved_at: "2026-11-03T00:00:00Z", mode: "selected", owners };
const request = () => FederatedSearchRequest.create({ scope: { mode: "selected", owners }, max_results: 100,
  queries: [{ category: "message", operation: "content", contract_version: 1, query: "😀词", match_mode: "literal", limit: 50, file_targets: [], diagnostic_targets: [] }] });
const key = (index: number) => ({ owner: owners[index], category: "message", operation: "content" });
const branch = (index: number, state = "complete") => ({ branch: key(index), state, retryable: false,
  allocated_results: 50, returned_results: 0, ...(state === "limited" ? { code: "scan_limit", cursor: "original-owner-cursor" } : {}) });
const resolution = () => ({ scope, branches: owners.map(owner => ({ owner, state: "registered" })) });
const page = (index: number) => ({ provider_id: "canonical-history", workspace_id: owners[index].workspace_id,
  category: "message", operation: "content", contract_version: 1, state: "complete", messages: { catalog_revision: 1,
    items: [], cursor: null, complete: true, limited_reason: null, scanned_sessions: 1, scanned_threads: 1, scanned_items: 1, scanned_bytes: 5 } });
const summary = (branches = [branch(0), branch(1)]) => ({ state: branches.every(item => item.state === "complete") ? "complete" : "incomplete",
  order: "category-scope-owner-v1", branches, returned_results: 0 });
type Payload = Record<string, unknown>;
function install() {
  const calls: Array<{ body: ReturnType<typeof request>; signal?: AbortSignal | null }> = [];
  const encoder = new TextEncoder();
  let control!: ReadableStreamDefaultController<Uint8Array>, sequence = 0, cancelled = false;
  installSessionApiFetchMock(({ path, init }) => {
    expect(path).toBe("/api/gateway/search");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    calls.push({ body: JSON.parse(String(init?.body)), signal: init?.signal });
    return new Response(new ReadableStream<Uint8Array>({ start(controller) { control = controller; }, cancel() { cancelled = true; } }),
      { headers: { "Content-Type": "text/event-stream", "X-Request-ID": "original-request" } });
  });
  return { calls, get cancelled() { return cancelled; },
    send(type: string, payload: Payload = {}, overrides: Payload = {}) {
      const event = { type, sequence: ++sequence, request_id: "original-request", ...payload, ...overrides };
      control.enqueue(encoder.encode(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify(event)}\n\n`));
    }, close() { control.close(); } };
}
async function until(condition: () => boolean) {
  for (let index = 0; index < 100; index++) {
    if (condition()) return;
    await new Promise(resolve => setTimeout(resolve, 1));
  }
  throw new Error("搜索测试条件未收敛");
}
afterEach(unwrapSessionApiFetch);

test.each([false, true])("未收到完整终态时保留响应头请求身份：已收到范围=%s", async scoped => {
  const backend = install(), events: string[] = [];
  const outcome = streamFederatedSearch(49_357, request(), { signal: new AbortController().signal,
    onEvent: event => events.push(event.type) }).catch(error => error);
  await until(() => backend.calls.length === 1);
  if (scoped) backend.send("scope", { scope: resolution() });
  backend.close();
  const error = await outcome;
  expect(error).toBeInstanceOf(IncompleteSearchStreamError);
  expect(error).toMatchObject({ name: "IncompleteSearchStreamError", code: "incomplete_stream", requestId: "original-request" });
  expect(error.message).toBe("搜索连接已结束，但未收到完整终态");
  expect(events).toEqual(scoped ? ["scope"] : []);
  expect(backend.calls).toHaveLength(1);
});

test("同 owner 多根各自结算，续查只接受原文件根而不合并游标", async () => {
  const roots = ["one", "two"].map(name => `boxteam://workspace/A/resources/files/root/${name}`);
  const input = FederatedSearchRequest.create({ scope: { mode: "selected", owners }, max_results: 3,
    queries: [{ category: "file", operation: "name", contract_version: 1, query: "needle", match_mode: "fuzzy", limit: 1,
      file_targets: roots.map(uri => ({ owner: owners[0], target: { uri, max_bytes: 65536 } })), diagnostic_targets: [] }] });
  const branches = roots.map((resource_uri, index) => ({ ...branch(0, "limited"), cursor: `cursor-${index}`,
    branch: { owner: owners[0], category: "file", operation: "name", resource_uri } }));
  const backend = install(), received: string[] = [];
  const running = streamFederatedSearch(49_351, input, { signal: new AbortController().signal,
    onEvent: event => { if (event.type === "branch") received.push(event.branch!.branch!.resource_uri!); } });
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() });
  for (const item of branches) backend.send("branch", { branch: item });
  backend.send("end", { summary: summary(branches) }); backend.close();
  expect((await running).branches.map(item => item.cursor)).toEqual(["cursor-0", "cursor-1"]);
  expect(received).toEqual(roots);

  const followup = FederatedSearchRequest.create({ ...input, scope: { mode: "resume", scope, owners: [] },
    continuations: [{ branch: branches[1].branch, cursor: branches[1].cursor }] });
  const resumed = install();
  const next = streamFederatedSearch(49_352, followup, { signal: new AbortController().signal, onEvent() {} });
  await until(() => resumed.calls.length === 1);
  resumed.send("scope", { scope: resolution() }); resumed.send("branch", { branch: branches[1] });
  resumed.send("end", { summary: summary([branches[1]]) }); resumed.close();
  expect((await next).branches).toHaveLength(1);
  expect(resumed.calls[0].body.continuations).toEqual(followup.continuations);
});

test("一次请求指定 A+B，快分支在 B 完成前交付，结束汇总不重复投影正文", async () => {
  const backend = install(), events: string[] = [], controller = new AbortController();
  const running = streamFederatedSearch(49_351, request(), { signal: controller.signal, onEvent: event => events.push(event.type) });
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() });
  backend.send("page", { branch: branch(0), page: page(0) });
  backend.send("branch", { branch: branch(0) });
  await until(() => events.length === 3);
  expect(events).toEqual(["scope", "page", "branch"]);
  const unsupported = { ...branch(1, "failed"), code: "unsupported_category", message: "原 owner 尚不支持", retryable: false };
  backend.send("branch", { branch: unsupported });
  backend.send("end", { summary: summary([branch(0), unsupported]) }); backend.close();
  expect((await running).state).toBe("incomplete");
  expect(backend.calls).toHaveLength(1);
  expect(backend.calls[0].body.scope).toEqual({ mode: "selected", owners });
  expect(backend.calls[0].body.queries[0].category).toBe("message");
  expect(backend.calls[0].body.deadline_at).toBeGreaterThan(Date.now() / 1000);
  expect(events).toEqual(["scope", "page", "branch", "branch", "end"]);
});

test("续查只传冻结范围、B 的原 cursor 和本次查询，不预检或读取 A", async () => {
  const backend = install(), followup = request();
  followup.scope = { mode: "resume", scope, owners: [] };
  followup.continuations = [{ branch: key(1), cursor: "opaque-original-cursor" }];
  const running = streamFederatedSearch(49_352, followup, { signal: new AbortController().signal, onEvent() {} });
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() }); backend.send("branch", { branch: branch(1, "limited") });
  backend.send("end", { summary: summary([branch(1, "limited")]) }); backend.close();
  expect((await running).branches[0].cursor).toBe("original-owner-cursor");
  expect(backend.calls[0].body.scope).toEqual(followup.scope);
  expect(backend.calls[0].body.continuations).toEqual(followup.continuations);
});

test.each(["断流", "重复终态", "错误请求", "跳序", "字段非法", "错误归属", "范围改写", "缺分支", "结束后事件"])("流协议错误明确失败并回收读取：%s", async kind => {
  const backend = install();
  const outcome = streamFederatedSearch(49_353, request(), { signal: new AbortController().signal, onEvent() {} }).catch(error => error as Error);
  await until(() => backend.calls.length === 1);
  if (kind === "范围改写") backend.send("scope", { scope: { ...resolution(), scope: { ...scope, owners: [owners[1], owners[0]] } } });
  else backend.send("scope", { scope: resolution() });
  if (kind === "错误请求") backend.send("branch", { branch: branch(0) }, { request_id: "another-request" });
  if (kind === "跳序") backend.send("branch", { branch: branch(0) }, { sequence: 9 });
  if (kind === "字段非法") backend.send("page", { branch: branch(0), page: { ...page(0), contract_version: "invalid" } });
  if (kind === "错误归属") backend.send("page", { branch: branch(0), page: page(1) });
  if (kind === "重复终态") { backend.send("branch", { branch: branch(0) }); backend.send("branch", { branch: branch(0) }); }
  if (kind === "缺分支") backend.send("end", { summary: summary() });
  if (kind === "结束后事件") {
    backend.send("branch", { branch: branch(0) }); backend.send("branch", { branch: branch(1) });
    backend.send("end", { summary: summary() }); backend.send("branch", { branch: branch(1) });
  }
  if (kind === "断流") backend.close();
  const error = await outcome;
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toMatch(/搜索|校验/);
  if (kind !== "断流") expect(backend.cancelled).toBe(true);
});

test("取消后不消费迟到事件，传输与 reader 都释放", async () => {
  const backend = install(), controller = new AbortController(), events: string[] = [];
  const outcome = streamFederatedSearch(49_354, request(), { signal: controller.signal, onEvent: event => events.push(event.type) }).catch(error => error);
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() }); await until(() => events.length === 1);
  controller.abort();
  expect(await outcome).toBeInstanceOf(DOMException);
  expect(backend.calls[0].signal?.aborted).toBe(true);
  expect(backend.cancelled).toBe(true);
  expect(events).toEqual(["scope"]);
});

test("总期限包含正文读取，心跳不能延长期限", async () => {
  const backend = install(), input = request(); input.deadline_at = Date.now() / 1000 + 0.05;
  const outcome = streamFederatedSearch(49_355, input, { signal: new AbortController().signal, onEvent() {} }).catch(error => error);
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() });
  const error = await outcome;
  expect(error).toBeInstanceOf(DOMException);
  expect(error.name).toBe("TimeoutError");
  expect(error.message).toBe("搜索总期限已到");
  expect(backend.cancelled).toBe(true);
});

const diagnosticTarget = () => ({ gateway_id: "gateway", scope: { source: "workspace", workspace_ids: ["A", "B"] } });
const diagnosticRequest = () => FederatedSearchRequest.create({ scope: { mode: "selected", owners },
  queries: [{ category: "runtime_log", operation: "content", contract_version: 1, match_mode: "literal", query: "😀词", limit: 1,
    diagnostic_targets: [diagnosticTarget()] }] });
const diagnosticBranch = () => ({ branch: { owner: undefined, diagnostics: diagnosticTarget(), category: "runtime_log", operation: "content" },
  state: "limited", retryable: false, allocated_results: 1, returned_results: 1, cursor: "original-log-cursor", code: "result_limit" });
const diagnosticPage = () => {
  const source = { log_id: "shared-backend", source: "workspace", service: "workspace_api", label: "共享 Backend",
    workspace_ids: ["A", "B"], status: "available" };
  return { gateway_id: "gateway", scope: diagnosticTarget().scope, query: "😀词", sources: [source],
    items: [{ source, reference: { gateway_id: "gateway", log_id: source.log_id, device: "1", inode: "2", line_start: 0,
      line_end: 20, content_version: "a".repeat(64), start_codepoint: 2, end_codepoint: 4 }, preview: "开头😀词", preview_start_codepoint: 0 }],
    complete: false, cursor: "original-log-cursor", limited_reason: "result_limit", scanned_sources: 1, scanned_lines: 1, scanned_bytes: 20 };
};
const diagnosticSummary = () => ({ state: "incomplete", order: "category-scope-owner-v1", branches: [diagnosticBranch()], returned_results: 1 });

test("运行日志每 Gateway 只有真实诊断分支，共享来源及原 cursor 通过同一流", async () => {
  const backend = install(), received: string[] = [];
  const running = streamFederatedSearch(49_356, diagnosticRequest(), { signal: new AbortController().signal,
    onEvent: event => received.push(event.type) });
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() });
  backend.send("page", { branch: diagnosticBranch(), diagnostic_logs: diagnosticPage() });
  backend.send("branch", { branch: diagnosticBranch() });
  backend.send("end", { summary: diagnosticSummary() }); backend.close();
  expect((await running).branches[0].cursor).toBe("original-log-cursor");
  expect(received).toEqual(["scope", "page", "branch", "end"]);
  expect(backend.calls[0].body.queries[0].diagnostic_targets).toEqual([diagnosticTarget()]);
  const nextRequest = diagnosticRequest();
  nextRequest.scope = { mode: "resume", scope, owners: [] };
  nextRequest.continuations = [{ branch: diagnosticBranch().branch, cursor: "original-log-cursor" }];
  const resumed = install();
  const next = streamFederatedSearch(49_357, nextRequest, { signal: new AbortController().signal, onEvent() {} });
  await until(() => resumed.calls.length === 1);
  resumed.send("scope", { scope: resolution() });
  resumed.send("page", { branch: diagnosticBranch(), diagnostic_logs: diagnosticPage() });
  resumed.send("branch", { branch: diagnosticBranch() });
  resumed.send("end", { summary: diagnosticSummary() }); resumed.close();
  expect((await next).branches).toHaveLength(1);
  expect(resumed.calls[0].body.continuations).toEqual(nextRequest.continuations);
});

test.each(["query", "scope", "source", "reference", "duplicate", "wrong_payload", "outside_scope"])("运行日志拒绝非法领域页并释放流：%s", async damage => {
  const backend = install(), input = diagnosticRequest(), data = diagnosticPage();
  if (damage === "outside_scope") input.queries[0].diagnostic_targets[0].scope!.workspace_ids = ["C"];
  const outcome = streamFederatedSearch(49_358, input, { signal: new AbortController().signal, onEvent() {} }).catch(error => error);
  await until(() => backend.calls.length === 1);
  backend.send("scope", { scope: resolution() });
  if (damage === "query") data.query = "其它词";
  if (damage === "scope") data.scope = { source: "gateway", workspace_ids: [] };
  if (damage === "source") data.sources[0].workspace_ids = ["C"];
  if (damage === "reference") data.items[0].reference.gateway_id = "other-gateway";
  if (damage !== "outside_scope") backend.send("page", { branch: diagnosticBranch(), ...(damage === "wrong_payload" ? { page: page(0) } : { diagnostic_logs: data }) });
  if (damage === "duplicate") backend.send("page", { branch: diagnosticBranch(), diagnostic_logs: data });
  expect(await outcome).toBeInstanceOf(Error);
  expect(backend.cancelled).toBe(true);
});
