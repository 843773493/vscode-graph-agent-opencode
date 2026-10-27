import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/mcpConnections";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { useMcpConnections } from "./useMcpConnections";

const target: GatewayWorkspaceRequestTarget = { workspace_id: "gateway-one", backend_workspace_id: "backend-one", connection_kind: "local" };
const operation: api.McpConnectionOperationDTO = { operation_id: "original", connection_id: "connection", server_id: "secure", action: "connect", status: "waiting_user", stage: "authorize", created_at: 1, updated_at: 1, deadline: Date.now() / 1000 + 60 };
const server: api.McpServerDTO = { server_id: "secure", status: "authorization_required", transport: "streamable_http", enabled: true, tools: [] };
let renderer: ReactTestRenderer;
let value: ReturnType<typeof useMcpConnections>;
let poll: () => void;
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const restores: Array<() => void> = [];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}
function Probe({ owner }: { owner: GatewayWorkspaceRequestTarget }) {
  value = useMcpConnections(8014, owner);
  return null;
}
async function mount() { await act(async () => { renderer = create(<Probe owner={target} />); }); }

beforeEach(() => {
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    setInterval: (callback: () => void) => { poll = callback; return 1; }, clearInterval: () => {},
  } });
  const servers = spyOn(api, "listMcpServers").mockResolvedValue([server]);
  const operations = spyOn(api, "listMcpOperations").mockResolvedValue([operation]);
  restores.push(() => servers.mockRestore(), () => operations.mockRestore());
});
afterEach(() => {
  act(() => renderer?.unmount());
  for (const restore of restores.splice(0)) restore();
  if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
  else Reflect.deleteProperty(globalThis, "window");
});

test("同名 Server 切换 backend 后丢弃旧在途读取，旧动作不可再提交", async () => {
  const pending = deferred<api.McpServerDTO[]>();
  let oldSignal: AbortSignal | undefined;
  spyOn(api, "listMcpServers").mockImplementation(async (_port, owner, signal) => {
    if (owner.backend_workspace_id === "backend-one") { oldSignal = signal; return pending.promise; }
    return [{ ...server, status: "ready" }];
  });
  const submit = spyOn(api, "submitMcpOperation").mockResolvedValue(operation);
  restores.push(() => submit.mockRestore());
  await mount();
  const old = value;
  await act(async () => { renderer.update(<Probe owner={{ ...target, backend_workspace_id: "backend-two" }} />); });
  expect(oldSignal?.aborted).toBe(true);
  expect(value.servers[0].status).toBe("ready");
  await act(async () => { pending.resolve([server]); await old.submit("secure", "connect"); });
  expect(value.servers[0].status).toBe("ready");
  expect(submit).not.toHaveBeenCalled();
});

test("失败写入回读完整 owner 对象，不伪造连接成功", async () => {
  const submit = spyOn(api, "submitMcpOperation").mockRejectedValue(new Error("实际连接拒绝"));
  restores.push(() => submit.mockRestore());
  await mount();
  spyOn(api, "listMcpOperations").mockResolvedValue([{ ...operation, status: "failed", stage: "initialize", error_code: "initialize_failed" }]);
  await act(async () => { await value.submit("secure", "connect"); });
  expect(value.error).toContain("实际连接拒绝");
  expect(value.operations).toEqual([{ ...operation, status: "failed", stage: "initialize", error_code: "initialize_failed" }]);
  expect(value.servers[0].status).toBe("authorization_required");
  expect(value.busy).toBe(false);
});

test("慢读取不被轮询反复作废，动作后的旧读取不能覆盖回执", async () => {
  const pending = deferred<api.McpConnectionOperationDTO[]>();
  const operations = spyOn(api, "listMcpOperations").mockReturnValueOnce(pending.promise);
  const submit = spyOn(api, "submitMcpOperation").mockResolvedValue({ ...operation, status: "succeeded" });
  restores.push(() => submit.mockRestore());
  await mount();
  await act(async () => { poll(); poll(); });
  expect(operations).toHaveBeenCalledTimes(1);
  operations.mockResolvedValue([{ ...operation, status: "succeeded" }]);
  await act(async () => { await value.submit("secure", "connect"); pending.resolve([operation]); });
  expect(value.operations[0].status).toBe("succeeded");
});
