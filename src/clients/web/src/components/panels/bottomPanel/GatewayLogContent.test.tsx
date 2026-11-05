import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import {
  installGatewayFetch,
  restoreGatewayTestEnvironment,
} from "../../../tests/fixtures/session/gatewayEnvironment";
import { GatewayLogContent } from "./GatewayLogContent";
import type { ReactNode } from "react";
import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";

afterEach(() => restoreGatewayTestEnvironment());

const renderToolbar = (tools: ReactNode) => <header>{tools}</header>;
const workspaces = [
  { gatewayId: "gateway_00000000000040008000000000000001", workspaceId: "00000000-0000-4000-8000-000000000001", label: "ws_a" },
  { gatewayId: "gateway_00000000000040008000000000000002", workspaceId: "00000000-0000-4000-8000-000000000001", label: "ws_b" },
];
const owners = workspaces.map(({ gatewayId, workspaceId }) => ({ gatewayId, workspaceId }));
const workspaceKeys = workspaces.map(gatewayWorkspaceKey);

/** GatewayLogContent 会注册 3 秒轮询，测试环境必须显式提供 setInterval。 */
function installPollingWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      setInterval: () => 0,
      clearInterval: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

function diagnosticsPayload(workspace: typeof workspaces[number]) {
  const { gatewayId, workspaceId, label } = workspace;
  return {
    gateway_id: gatewayId,
    gateway_name: "本机 Gateway",
    gateway_connection_id: null,
    connection_kind: "local",
    status: "ready",
    checked_at: "2026-08-02T00:00:00Z",
    selected_workspace_id: workspaceId,
    selected_log_id: `log_${label}`,
    workspaces: [],
    logs: [
      {
        log_id: `log_${label}`,
        source: "workspace",
        workspace_id: workspaceId,
        workspace_name: label,
        service: "workspace",
        label: `输出-${label}`,
        status: "available",
        tail: `TAIL_${label}`,
        truncated: false,
        line_count: 1,
        size_bytes: 10,
        updated_at: "2026-08-02T00:00:00Z",
        error: null,
      },
    ],
  };
}

interface PendingDiagnosticsRequest {
  owner: { gatewayId: string; workspaceId: string };
  resolve: () => void;
}

function installPendingDiagnosticsFetch(token: string): PendingDiagnosticsRequest[] {
  const pendingRequests: PendingDiagnosticsRequest[] = [];
  installGatewayFetch(({ url, path, method, init }) => {
    if (path !== "/api/gateway/diagnostics" || (method && method !== "GET")) return undefined;
    const workspaceId = new URL(url, "http://localhost").searchParams.get("workspace_id");
    if (!workspaceId) return undefined;
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(workspaceId);
    const workspace = workspaces.find(item => item.gatewayId === headers.get("X-BoxTeam-Target-Gateway-Id") && item.workspaceId === workspaceId);
    if (!workspace) throw new Error("输出装具收到未声明的完整工作区 owner");
    return new Promise<Response>((resolve) => {
      pendingRequests.push({
        owner: { gatewayId: workspace.gatewayId, workspaceId },
        resolve: () =>
          resolve(
            Response.json({
              data: diagnosticsPayload(workspace),
              request_id: `req_${workspaceId}`,
            }),
          ),
      });
    });
  }, { token });
  return pendingRequests;
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("切换工作区后到达的旧输出不得覆盖新工作区", async () => {
  // 底部输出面板跟随当前工作区；工作区切换会重发诊断请求。慢的旧请求若在快的
  // 新请求之后返回，必须被丢弃，否则用户切到新工作区后仍看到上一个工作区的日志。
  const pendingRequests = installPendingDiagnosticsFetch("log-race-token");
  installPollingWindow(8025);

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayLogContent apiPort={8025} workspaceId={workspaceKeys[0]} active renderToolbar={renderToolbar} />,
    );
  });
  await flush();

  // 第一次请求仍在途时切换工作区，触发第二次请求。
  act(() => {
    renderer.update(
      <GatewayLogContent apiPort={8025} workspaceId={workspaceKeys[1]} active renderToolbar={renderToolbar} />,
    );
  });
  await flush();
  expect(pendingRequests.map((request) => request.owner)).toEqual(owners);

  // 新工作区先返回，旧工作区后返回。
  await act(async () => {
    pendingRequests[1].resolve();
    await Promise.resolve();
  });
  await act(async () => {
    pendingRequests[0].resolve();
    await Promise.resolve();
  });

  const text = JSON.stringify(renderer.toJSON());
  expect(text).toContain("TAIL_ws_b");
  expect(text).not.toContain("TAIL_ws_a");
  act(() => renderer.unmount());
});
test("切换工作区到新工作区期间不得继续渲染旧工作区日志", async () => {
  // 切换窗口保护：A 已显示后切到 B，B 的响应仍在途时，面板必须立刻作废 A 的快照，
  // 不得因为 getWorkspaceLogs 回退到旧 diagnostics.selected_workspace_id 而继续渲染 A 的 tail，
  // 也不得把这段空窗期渲染成读取失败结论。
  const pendingRequests = installPendingDiagnosticsFetch("log-switch-window-token");
  installPollingWindow(8028);

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayLogContent apiPort={8028} workspaceId={workspaceKeys[0]} active renderToolbar={renderToolbar} />,
    );
  });
  await flush();
  await act(async () => {
    pendingRequests[0].resolve();
    await Promise.resolve();
  });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).toContain("TAIL_ws_a");

  // 切到 ws_b：该工作区的诊断请求仍在途。
  act(() => {
    renderer.update(
      <GatewayLogContent apiPort={8028} workspaceId={workspaceKeys[1]} active renderToolbar={renderToolbar} />,
    );
  });
  await flush();
  const during = JSON.stringify(renderer.toJSON());
  expect(during).not.toContain("TAIL_ws_a");
  expect(during).not.toContain("工作区输出读取失败");
  expect(during).not.toContain("请刷新后重试");

  // ws_b 到达后正常渲染，证明上面是切换空窗而不是把面板彻底卡死。
  expect(pendingRequests.map((request) => request.owner)).toEqual(owners);
  await act(async () => {
    pendingRequests[1].resolve();
    await Promise.resolve();
  });
  await flush();
  const after = JSON.stringify(renderer.toJSON());
  expect(after).toContain("TAIL_ws_b");
  expect(after).not.toContain("TAIL_ws_a");
  act(() => renderer.unmount());
});

test("原生宿主隐藏输出时停止读取，重新展开保留筛选输入", async () => {
  let reads = 0;
  installGatewayFetch(({ path, url, init }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(owners[0].gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(owners[0].workspaceId);
    expect(new URL(url, "http://localhost").searchParams.get("workspace_id")).toBe(owners[0].workspaceId);
    reads += 1;
    return Response.json({ data: diagnosticsPayload(workspaces[0]), request_id: "req_visibility" });
  }, { token: "visibility-token" });
  installPollingWindow(8025);
  const view = (active: boolean) => <GatewayLogContent apiPort={8025} workspaceId={workspaceKeys[0]} active={active} renderToolbar={tools => <div>{tools}</div>} />;
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(view(false)); });
  expect(reads).toBe(0);
  await act(async () => { renderer.update(view(true)); });
  await flush();
  const input = () => renderer.root.findByProps({ "aria-label": "筛选当前输出" });
  act(() => input().props.onChange({ target: { value: "TAIL" } }));
  await act(async () => { renderer.update(view(false)); });
  const previous = reads;
  await flush();
  expect(reads).toBe(previous);
  await act(async () => { renderer.update(view(true)); });
  await flush();
  expect(reads).toBeGreaterThan(previous);
  expect(input().props.value).toBe("TAIL");
  act(() => renderer.unmount());
});
