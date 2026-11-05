import { afterEach, expect, test } from "bun:test";
import {
  cancelMcpOperation, completeMcpAuthorization, getMcpAuthorization,
  listMcpOperations, getMcpCatalog, submitMcpOperation,
  getMcpInstallationCapabilities, inspectMcpInstallationCandidate, planMcpInstallation, installMcpArtifact, getMcpInstallationOperation,
} from "./mcpConnections";
import type { McpConnectionOperationDTO, McpServerDTO } from "./mcpConnections";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";

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
  expect(requests.map(({ url }) => url.pathname)).toEqual([
    "/api/v1/mcp/installation-capabilities", "/api/v1/mcp/candidate-manifests", "/api/v1/mcp/candidate-plans",
    "/api/v1/mcp/installations", "/api/v1/mcp/installation-operations/original%2Fid",
  ]);
  expect(JSON.parse(String(requests[3]!.init?.body))).toEqual(intent);
  for (const { init } of requests) {
    expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(target.gateway_id);
    expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
  }
  actual = { ...native, backend_id: "00000000-0000-4000-8000-000000000012" };
  await expect(getMcpInstallationOperation(port, target, native, "original")).rejects.toThrow("Backend 已改变");
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
