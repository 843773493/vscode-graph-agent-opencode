import { afterEach, expect, test } from "bun:test";
import { FederatedSearchRequest } from "../../types/protocol_generated/boxteam/search/v1/public";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "../session/sessionApiFetchMock";
import { streamFederatedSearch } from "./federatedSearch";

const owners = ["A", "B"].map(workspace_id => ({ gateway_id: "gateway", workspace_id }));
const scope = { version: 1, gateway_id: "gateway", resolved_at: "2026-11-03T00:00:00Z", mode: "selected", owners };
const request = () => FederatedSearchRequest.create({ scope: { mode: "selected", owners }, max_results: 100,
  queries: [{ category: "message", operation: "content", contract_version: 1, query: "😀词", match_mode: "literal", limit: 50 }] });
const key = (index: number) => ({ owner: owners[index], category: "message", operation: "content" });
const branch = (index: number, state = "complete") => ({ branch: key(index), state, retryable: false,
  allocated_results: 50, returned_results: 0, ...(state === "limited" ? { code: "scan_limit", cursor: "original-owner-cursor" } : {}) });
const resolution = () => ({ scope, branches: owners.map(owner => ({ owner, state: "registered", target_id: owner.workspace_id })) });
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
  expect((await outcome).message).toBe("搜索总期限已到");
  expect(backend.cancelled).toBe(true);
});
