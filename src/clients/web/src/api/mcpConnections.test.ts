import { afterEach, expect, test } from "bun:test";
import {
  cancelMcpOperation, completeMcpAuthorization, getMcpAuthorization,
  listMcpOperations, getMcpCatalog, submitMcpOperation,
  getMcpInstallationCapabilities, inspectMcpInstallationCandidate, planMcpInstallation, installMcpArtifact, getMcpInstallationOperation, cancelMcpInstallationOperation,
  writeMcpPrivateHeader, writeMcpPrivateEnv, draftMcpCandidateConfiguration,
  getMcpArtifactReferences, uninstallMcpArtifact,
} from "./mcpConnections";
import type { McpConnectionOperationDTO, McpServerDTO } from "./mcpConnections";
import type { McpServerListResponseDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { onGatewayUserSessionInvalidated } from "./http";

const originalFetch = globalThis.fetch;
const target: GatewayWorkspaceRequestTarget = {
  gateway_id: "gateway_00000000000040008000000000000001",
  workspace_id: "00000000-0000-4000-8000-000000000001",
};
const otherGateway = "gateway_00000000000040008000000000000002";
const revision = "sha256:" + "a".repeat(64);
let nextPort = 59_380;
const operation: McpConnectionOperationDTO = {
  operation_id: "operation-id", connection_id: "connection-id", server_id: "server", action: "connect",
  status: "pending", stage: "queued", created_at: 1, deadline: 30, updated_at: 1,
};

function envelope(data: unknown, extra: Record<string, unknown> = {}) {
  return { code: 0, message: "ok", data, request_id: "req-mcp", target: { kind: "workspace", ...target }, protocol_version: 1, ...extra };
}

function installFetch(respond: (url: URL, init: RequestInit | undefined) => unknown): number {
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    const url = new URL(String(input));
    if (url.pathname === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "mcp-test-token" } });
    if (url.pathname === "/api/gateway/users/current") return Response.json({ data: {}, request_id: "req-user" });
    return Response.json(respond(url, init));
  }, { preconnect: originalFetch.preconnect });
  return nextPort++;
}

afterEach(() => { globalThis.fetch = originalFetch; });

test("配置提案消费原脱敏JSONC，冻结来源与幂等键，拒绝异主回执", async () => {
  const edit = { ...target, source_key: "workspace_root_mutable_override", jsonc: "// 原件\n{}", idempotency_key: "draft-operation",
    base_file_digest: "base", base_layer_revision: 1, base_layer_digest: "layer", expected_active_revision: 1, expected_active_digest: "active" };
  const payload = { target: { kind: "workspace", ...target }, server_id: "secure", enabled: false, source_edit: edit,
    plan: { candidate: { server_id: "secure", target: { kind: "backend", gateway_id: target.gateway_id,
      backend_id: "00000000-0000-4000-8000-000000000011" },
      source: { kind: "manual", name: "secure", version: "1", definition: { transport: "stdio", command: "/target/python" } },
      manifest_digest: "sha256:jcs:v1:" + "a".repeat(64),
      variant: { kind: "manual", index: 0, digest: "sha256:jcs:v1:" + "b".repeat(64) }, inputs: {} },
    plan_digest: "sha256:jcs:v1:" + "c".repeat(64) } };
  const draft = { source_edit: { ...edit, jsonc: '{"mcp":{"servers":{"secure":{"enabled":false}}}}' },
    preflight: { document: { ...target, source_key: edit.source_key } } };
  let actual = draft;
  const requests: RequestInit[] = [];
  const port = installFetch((url, init) => {
    expect(url.pathname).toBe("/api/v1/mcp/candidate-configurations/draft");
    expect(init?.method).toBe("POST"); expect(init?.cache).toBe("no-store");
    requests.push(init!);
    return envelope(actual);
  });
  expect(JSON.stringify(await draftMcpCandidateConfiguration(port, target, payload))).toBe(JSON.stringify(draft));
  expect(JSON.parse(String(requests[0]!.body))).toEqual(payload);
  for (const change of [{ workspace_id: "other" }, { source_key: "inline" }, { idempotency_key: "another-operation" }]) {
    actual = { ...draft, source_edit: { ...draft.source_edit, ...change } };
    await expect(draftMcpCandidateConfiguration(port, target, payload)).rejects.toThrow("原目标或来源");
  }
  actual = draft;
  const pending = draftMcpCandidateConfiguration(port, { ...target }, payload);
  payload.source_edit.source_key = "changed";
  expect(JSON.stringify(await pending)).toBe(JSON.stringify(draft));
});

test("私有 env 按手动或原目录请求选择能力路由，只消费精确引用", async () => {
  const payload = { target: { kind: "workspace", ...target }, server_id: "secure", input_key: "API_KEY",
    launch: { command: "/tools/server", args: [] }, value: "private-env-client-only" };
  const reference = { kind: "mcp_private_env", workspace_id: target.workspace_id, server_id: "secure", input_key: "API_KEY",
    definition_digest: "sha256:jcs:v1:" + "a".repeat(64), version: "b".repeat(32) };
  const paths: string[] = [];
  const port = installFetch((url, init) => {
    paths.push(url.pathname);
    expect(JSON.parse(String(init?.body)).value).toBe(payload.value);
    return envelope(reference);
  });
  expect(await writeMcpPrivateEnv(port, target, payload)).toEqual(reference);
  const candidate = { target: { kind: "backend", gateway_id: target.gateway_id, backend_id: "00000000-0000-4000-8000-000000000011" },
    source: { kind: "gallery", name: "server", version: "1", protocol: "v0.1", endpoint: "https://registry.test", entry_url: "https://registry.test/server" },
    manifest_digest: "sha256:jcs:v1:" + "a".repeat(64), variant: { kind: "package", index: 0, digest: "sha256:jcs:v1:" + "b".repeat(64) },
    inputs: {}, server_id: "secure" };
  expect(await writeMcpPrivateEnv(port, target, { ...payload, candidate, launch: undefined })).toEqual(reference);
  expect(paths).toEqual(["/api/v1/mcp/private-env", "/api/v1/mcp/candidate-plans/private-env"]);
  const wrong = installFetch(() => envelope({ ...reference, input_key: "OTHER" }));
  await expect(writeMcpPrivateEnv(wrong, target, payload)).rejects.toThrow("原输入身份");
});

test("私有 header 只交给精确工作区，核对原输入和不透明版本后消费引用", async () => {
  const payload = { target: { kind: "workspace", ...target }, server_id: "secure", header_name: "Authorization",
    endpoint: "https://service.test/mcp?region=1", value: "Bearer private-client-unit-only" };
  const reference = { kind: "mcp_private_header", workspace_id: target.workspace_id, server_id: payload.server_id,
    header_name: payload.header_name, endpoint: payload.endpoint, version: "a".repeat(32) };
  const requests: RequestInit[] = [];
  let current = reference;
  const port = installFetch((url, init) => {
    expect(url.pathname).toBe("/api/v1/mcp/private-headers");
    expect(init?.method).toBe("POST");
    expect(init?.cache).toBe("no-store");
    requests.push(init!);
    return envelope(current);
  });
  expect(await writeMcpPrivateHeader(port, target, payload)).toEqual(reference);
  expect(JSON.parse(String(requests[0]!.body))).toEqual(payload);
  expect(new Headers(requests[0]!.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
  for (const change of [{ endpoint: "https://other.test/mcp" }, { server_id: "other" }, { header_name: "authorization" },
    { version: "" }, { workspace_id: "00000000-0000-4000-8000-000000000099" }]) {
    current = { ...reference, ...change };
    await expect(writeMcpPrivateHeader(port, target, payload)).rejects.toThrow("原输入身份");
  }
  const before = requests.length;
  await expect(writeMcpPrivateHeader(port, target, { ...payload, target: { ...payload.target, gateway_id: otherGateway } }))
    .rejects.toThrow("原 Gateway");
  expect(requests).toHaveLength(before);
  current = reference;
  const mutableTarget = { ...target };
  const mutablePayload = { ...payload, target: { ...payload.target } };
  const pending = writeMcpPrivateHeader(port, mutableTarget, mutablePayload);
  mutableTarget.workspace_id = "00000000-0000-4000-8000-000000000099";
  mutablePayload.server_id = "changed";
  mutablePayload.endpoint = "https://changed.test/mcp";
  expect(await pending).toEqual(reference);
});

test("制品入口冻结原生 Backend，实际安装与查询沿相同目标且不替换为workspace响应", async () => {
  const native = { kind: "backend", gateway_id: target.gateway_id, backend_id: "00000000-0000-4000-8000-000000000011" };
  const requests: { url: URL; init: RequestInit | undefined }[] = [];
  let actual = native;
  const port = installFetch((url, init) => {
    requests.push({ url, init });
    return envelope({ status: "running" }, { target: actual });
  });
  const candidate = { target: native, source: { kind: "manual", name: "exact", version: "1", definition: { packages: [] } },
    manifest_digest: "sha256:jcs:v1:" + "a".repeat(64), variant: { kind: "package", index: 2, digest: "sha256:jcs:v1:" + "b".repeat(64) }, inputs: {} };
  await getMcpInstallationCapabilities(port, target);
  await inspectMcpInstallationCandidate(port, target, candidate);
  await planMcpInstallation(port, target, candidate);
  const intent = { candidate, plan_digest: "sha256:jcs:v1:" + "c".repeat(64), operation_id: "original", entrypoint: "selected-entry" };
  await installMcpArtifact(port, target, intent);
  await getMcpInstallationOperation(port, target, native, "original/id");
  await cancelMcpInstallationOperation(port, target, native, "original/id");
  expect(requests.map(({ url }) => url.pathname)).toEqual([
    "/api/v1/mcp/installation-capabilities", "/api/v1/mcp/candidate-manifests", "/api/v1/mcp/candidate-plans",
    "/api/v1/mcp/installations", "/api/v1/mcp/installation-operations/original%2Fid",
    "/api/v1/mcp/installation-operations/original%2Fid/cancel",
  ]);
  expect(JSON.parse(String(requests[3]!.init?.body))).toEqual(intent);
  expect(requests[5]!.init?.method).toBe("POST");
  expect(JSON.parse(String(requests[5]!.init?.body))).toEqual(native);
  for (const { init } of requests) {
    expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
  }
  actual = { ...native, backend_id: "00000000-0000-4000-8000-000000000012" };
  await expect(getMcpInstallationOperation(port, target, native, "original")).rejects.toThrow("Backend 已改变");
  await expect(cancelMcpInstallationOperation(port, target, native, "original")).rejects.toThrow("Backend 已改变");
});

test("制品引用核原generation，卸载向原安装域传递完整binding与唯一操作", async () => {
  const native = { kind: "backend", gateway_id: target.gateway_id, backend_id: "00000000-0000-4000-8000-000000000011" };
  const binding = { backend_id: native.backend_id, installation_id: "artifact/id", generation: "original", artifact_digest: revision };
  let actual = binding;
  const requests: { url: URL; init: RequestInit | undefined }[] = [];
  const port = installFetch((url, init) => {
    requests.push({ url, init });
    return envelope({ binding: actual, references: [] }, { target: native });
  });
  expect((await getMcpArtifactReferences(port, target, native, binding)).data?.binding).toEqual(binding);
  const intent = { target: native, binding, operation_id: "one-removal" };
  await uninstallMcpArtifact(port, target, intent);
  expect(requests[0]!.url.pathname).toBe("/api/v1/mcp/installations/artifact%2Fid/references");
  expect(requests[1]!.url.pathname).toBe("/api/v1/mcp/installations/artifact%2Fid");
  expect(requests[1]!.init?.method).toBe("DELETE");
  expect(JSON.parse(String(requests[1]!.init?.body))).toEqual(intent);
  for (const { init } of requests) {
    expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    expect(init?.cache).toBe("no-store");
  }
  actual = { ...binding, generation: "different" };
  await expect(getMcpArtifactReferences(port, target, native, binding)).rejects.toThrow("原 generation");
});

test("卸载收到会话401触发原身份屏障后，真实HTTP恢复链不重发旧确认", async () => {
  const port = nextPort++;
  const controller = new AbortController();
  const release = onGatewayUserSessionInvalidated(port, () => controller.abort(new Error("原确认身份已失效")));
  const native = { kind: "backend", gateway_id: target.gateway_id, backend_id: "00000000-0000-4000-8000-000000000011" };
  const binding = { backend_id: native.backend_id, installation_id: "artifact", generation: "original", artifact_digest: revision };
  let deletions = 0, sessions = 0;
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args, path = new URL(String(input)).pathname;
    if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "mcp-test-token" } });
    if (path === "/api/gateway/users/current") {
      ++sessions;
      return Response.json({ data: { kind: "guest", user_id: null }, request_id: "recovered-identity" });
    }
    expect(path).toBe("/api/v1/mcp/installations/artifact");
    expect(init?.method).toBe("DELETE");
    expect(JSON.parse(String(init?.body))).toEqual({ target: native, binding, operation_id: "old-confirmation" });
    ++deletions;
    return Response.json({ detail: "user_session_required" }, { status: 401, headers: { "X-Request-ID": "expired-identity" } });
  }, { preconnect: originalFetch.preconnect });
  try {
    const failure = await uninstallMcpArtifact(port, target, { target: native, binding, operation_id: "old-confirmation" }, controller.signal)
      .catch((cause: unknown) => cause);
    expect(failure).toBe(controller.signal.reason);
    expect(controller.signal.aborted).toBe(true);
    expect(sessions).toBe(2);
    expect(deletions).toBe(1);
  } finally { release(); }
});

test("所有 MCP 消费者传递完整 owner，保留完整目录封套及原操作对象", async () => {
  const requests: { url: URL; init: RequestInit | undefined }[] = [];
  const servers: McpServerDTO[] = [{ server_id: "server", transport: "stdio", enabled: true, status: "ready", tools: [], connection_ref: "mcp-connection:v1:" + "a".repeat(32) + ":1" }];
  const authorization = { authorization_url: "http://localhost/authorize" };
  const port = installFetch((url, init) => {
    requests.push({ url, init });
    if (url.pathname.endsWith("/servers")) return envelope(servers, { catalog_revision: revision, catalog_generation: 1 });
    if (url.pathname.endsWith("/authorization")) return envelope(authorization);
    return envelope(init?.method ? operation : [operation]);
  });
  const signal = new AbortController().signal;
  const catalog = { ...envelope(servers), data: servers, target: { kind: "workspace" as const, ...target }, protocol_version: 1 as const, catalog_revision: revision, catalog_generation: 1 };
  expect(await getMcpCatalog(port, target)).toEqual(catalog);
  expect(await listMcpOperations(port, target)).toEqual([operation]);
  expect(await submitMcpOperation(port, target, "server", "connect", signal)).toEqual(operation);
  expect(await cancelMcpOperation(port, target, "operation/id", signal)).toEqual(operation);
  expect(await getMcpAuthorization(port, target, "operation/id", signal)).toEqual(authorization);
  expect(await completeMcpAuthorization(port, target, "operation/id", { code: "private-code", state: "private-state" }, signal)).toEqual(operation);
  expect(requests).toHaveLength(6);
  expect(requests[0]!.url.search).toBe("?refresh=true");
  for (const { init } of requests) {
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    expect(init?.cache).toBe("no-store");
  }
  expect(requests[3]!.url.pathname).toEndWith("/operation%2Fid/cancel");
  const intent = JSON.parse(String(requests[2]!.init?.body)) as Record<string, unknown>;
  expect(intent).toMatchObject({ server_id: "server", action: "connect" });
  expect(typeof intent.operation_id).toBe("string");
  expect(JSON.parse(String(requests[5]!.init?.body))).toEqual({ code: "private-code", state: "private-state" });
});

test("被动目录查询明确禁用远端刷新，保留取消信号、精确 owner 和完整信封", async () => {
  const requests: { url: URL; init: RequestInit | undefined }[] = [];
  const catalog: McpServerListResponseDTO = {
    code: 0, message: "ok", request_id: "req-mcp", protocol_version: 1,
    target: { kind: "workspace", ...target }, data: [], catalog_revision: revision, catalog_generation: 2,
  };
  const port = installFetch((url, init) => { requests.push({ url, init }); return catalog; });
  const signal = new AbortController().signal;
  expect(await getMcpCatalog(port, target, { refresh: false, signal })).toEqual(catalog);
  expect(requests).toHaveLength(1);
  expect(requests[0]!.url.pathname).toBe("/api/v1/mcp/servers");
  expect(requests[0]!.url.search).toBe("?refresh=false");
  expect(requests[0]!.init?.signal).toBeInstanceOf(AbortSignal);
  expect(new Headers(requests[0]!.init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
  expect(new Headers(requests[0]!.init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
  expect(requests[0]!.init?.cache).toBe("no-store");
  const controller = new AbortController();
  const reason = new DOMException("目录观察已取消", "AbortError");
  const abortPort = installFetch((_url, init) => {
    expect(init?.signal?.aborted).toBe(false);
    controller.abort(reason);
    expect(init?.signal?.aborted).toBe(true);
    expect(init?.signal?.reason).toBe(reason);
    return catalog;
  });
  await expect(getMcpCatalog(abortPort, target, { refresh: false, signal: controller.signal })).rejects.toBe(reason);
});

test.each([
  { protocol_version: 2 },
  { target: { ...target, kind: "backend" } },
  { target: { ...target, kind: "workspace", gateway_id: otherGateway } },
  { target: { ...target, kind: "workspace", workspace_id: "00000000-0000-4000-8000-000000000002" } },
  { target: { ...target, kind: "workspace", gateway_id: null } },
  { target: null },
])("空列表也拒绝不受支持或不属于请求 owner 的信封 %j", async invalid => {
  const port = installFetch(() => envelope([], invalid));
  await expect(listMcpOperations(port, target)).rejects.toThrow("MCP 管理响应");
});

test.each([
  { catalog_revision: "unknown", catalog_generation: 1 },
  { catalog_revision: revision, catalog_generation: 0 },
  { catalog_revision: revision, catalog_generation: 1.5 },
  { catalog_revision: revision, catalog_generation: Number.MAX_SAFE_INTEGER + 1 },
])("目录消费前拒绝无效 revision 或 generation %j", async invalid => {
  const port = installFetch(() => envelope([], invalid));
  await expect(getMcpCatalog(port, target)).rejects.toThrow("目录 revision／generation");
});

test("请求发出后选择对象改变不改变原 owner 核对基线", async () => {
  const selection = { ...target };
  const port = installFetch(() => envelope([]));
  const pending = listMcpOperations(port, selection);
  selection.gateway_id = otherGateway;
  expect(await pending).toEqual([]);
});

test("保留原信封的 request_id 与非空 data 校验", async () => {
  const port = installFetch(() => envelope(null));
  await expect(cancelMcpOperation(port, target, "operation-id", new AbortController().signal)).rejects.toThrow("缺少 data");
  const missingRequest = installFetch(() => envelope([], { request_id: "" }));
  await expect(listMcpOperations(missingRequest, target)).rejects.toThrow("缺少 request_id");
});
