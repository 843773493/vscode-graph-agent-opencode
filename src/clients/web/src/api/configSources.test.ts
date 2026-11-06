import { afterEach, expect, test } from "bun:test";
import { getConfigSourceDocument } from "./configSources";

const originalFetch = globalThis.fetch;
const target = { gateway_id: "gateway_00000000000040008000000000000001", workspace_id: "00000000-0000-4000-8000-000000000001" };
const sourceKey = "workspace_root_mutable_override";
let port = 59_580;
afterEach(() => { globalThis.fetch = originalFetch; });

function respond(document: Record<string, unknown>, inspect: (url: URL, init?: RequestInit) => void = () => {}) {
  globalThis.fetch = Object.assign(async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.pathname === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "config-test-token" } });
    if (url.pathname === "/api/gateway/users/current") return Response.json({ data: {}, request_id: "config-user" });
    inspect(url, init);
    return Response.json({ code: 0, message: "ok", request_id: "source-read", data: document });
  }, { preconnect: originalFetch.preconnect });
  return port++;
}

test("来源读取保留原正文与全部CAS，查询原提交携带同一幂等键和完整owner", async () => {
  const document = { ...target, source_key: sourceKey, jsonc: "// 原正文\r\n{}\r\n", file_digest: "file", layer: "workspace",
    writable: true, file_presence: "present", source_generation: "generation", json_schema: {}, effective: {}, source_effective: {},
    layers: [], origins: [], status: undefined, layer_revision: 3, layer_digest: "layer", active_revision: 2, active_digest: "active" };
  const seen: URL[] = [];
  const controller = new AbortController();
  const current = respond(document, (url, init) => {
    seen.push(url);
    expect(init?.cache).toBe("no-store");
    expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });
  expect(await getConfigSourceDocument(current, target, sourceKey, { signal: controller.signal })).toEqual(document);
  const key = "original+/=提交";
  expect(await getConfigSourceDocument(current, target, sourceKey, { idempotencyKey: key, signal: controller.signal })).toEqual(document);
  expect(seen.map(url => url.pathname)).toEqual([`/api/v1/config/sources/${sourceKey}`, `/api/v1/config/sources/${sourceKey}`]);
  expect(seen[0]!.search).toBe("");
  expect(seen[1]!.searchParams.get("idempotency_key")).toBe(key);
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
