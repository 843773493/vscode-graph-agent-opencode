import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import {
  installGatewayFetch,
  restoreSessionHookGlobals,
} from "../../../hooks/session/sessionHookTestFixtures";
import GatewayLogPanel from "./GatewayLogPanel";

afterEach(() => restoreSessionHookGlobals());

/** GatewayLogPanel 会注册 3 秒轮询，测试环境必须显式提供 setInterval。 */
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

function diagnosticsPayload(workspaceId: string) {
  return {
    gateway_id: "gateway_local",
    gateway_name: "本机 Gateway",
    gateway_connection_id: null,
    connection_kind: "local",
    status: "ready",
    checked_at: "2026-08-02T00:00:00Z",
    selected_workspace_id: workspaceId,
    selected_log_id: `log_${workspaceId}`,
    workspaces: [],
    logs: [
      {
        log_id: `log_${workspaceId}`,
        source: "workspace",
        workspace_id: workspaceId,
        workspace_name: workspaceId,
        service: "workspace",
        label: `输出-${workspaceId}`,
        status: "available",
        tail: `TAIL_${workspaceId}`,
        truncated: false,
        line_count: 1,
        size_bytes: 10,
        updated_at: "2026-08-02T00:00:00Z",
        error: null,
      },
    ],
  };
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
  const pending: Array<{ workspaceId: string; resolve: () => void }> = [];
  installGatewayFetch(({ url, path }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    const workspaceId = new URL(url, "http://localhost").searchParams.get("workspace_id");
    if (!workspaceId) return undefined;
    return new Promise<Response>((resolve) => {
      pending.push({
        workspaceId,
        resolve: () =>
          resolve(
            Response.json({
              data: diagnosticsPayload(workspaceId),
              request_id: `req_${workspaceId}`,
            }),
          ),
      });
    });
  }, { token: "log-race-token" });
  installPollingWindow(8025);

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayLogPanel apiPort={8025} workspaceId="ws_a" height={200} onClose={() => undefined} />,
    );
  });
  await flush();

  // 第一次请求仍在途时切换工作区，触发第二次请求。
  act(() => {
    renderer.update(
      <GatewayLogPanel apiPort={8025} workspaceId="ws_b" height={200} onClose={() => undefined} />,
    );
  });
  await flush();
  expect(pending.map((item) => item.workspaceId)).toEqual(["ws_a", "ws_b"]);

  // 新工作区先返回，旧工作区后返回。
  await act(async () => {
    pending[1].resolve();
    await Promise.resolve();
  });
  await act(async () => {
    pending[0].resolve();
    await Promise.resolve();
  });

  const text = JSON.stringify(renderer.toJSON());
  expect(text).toContain("TAIL_ws_b");
  expect(text).not.toContain("TAIL_ws_a");
  renderer.unmount();
});
test("切换工作区到新工作区期间不得继续渲染旧工作区日志", async () => {
  // 切换窗口保护：A 已显示后切到 B，B 的响应仍在途时，面板必须立刻作废 A 的快照，
  // 不得因为 getWorkspaceLogs 回退到旧 diagnostics.selected_workspace_id 而继续渲染 A 的 tail，
  // 也不得把这段空窗期渲染成读取失败结论。
  const pending: Array<{ workspaceId: string; resolve: () => void }> = [];
  installGatewayFetch(({ url, path, method }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    if (method && method !== "GET") return undefined;
    const workspaceId = new URL(url, "http://localhost").searchParams.get("workspace_id");
    if (!workspaceId) return undefined;
    return new Promise<Response>((resolve) => {
      pending.push({
        workspaceId,
        resolve: () =>
          resolve(
            Response.json({
              data: diagnosticsPayload(workspaceId),
              request_id: `req_${workspaceId}`,
            }),
          ),
      });
    });
  }, { token: "log-switch-window-token" });
  installPollingWindow(8028);

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayLogPanel apiPort={8028} workspaceId="ws_a" height={200} onClose={() => undefined} />,
    );
  });
  await flush();
  await act(async () => {
    pending[0].resolve();
    await Promise.resolve();
  });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).toContain("TAIL_ws_a");

  // 切到 ws_b：该工作区的诊断请求仍在途。
  act(() => {
    renderer.update(
      <GatewayLogPanel apiPort={8028} workspaceId="ws_b" height={200} onClose={() => undefined} />,
    );
  });
  await flush();
  const during = JSON.stringify(renderer.toJSON());
  expect(during).not.toContain("TAIL_ws_a");
  expect(during).not.toContain("工作区输出读取失败");
  expect(during).not.toContain("请刷新后重试");

  // ws_b 到达后正常渲染，证明上面是切换空窗而不是把面板彻底卡死。
  expect(pending.map((item) => item.workspaceId)).toEqual(["ws_a", "ws_b"]);
  await act(async () => {
    pending[1].resolve();
    await Promise.resolve();
  });
  await flush();
  const after = JSON.stringify(renderer.toJSON());
  expect(after).toContain("TAIL_ws_b");
  expect(after).not.toContain("TAIL_ws_a");
  renderer.unmount();
});
