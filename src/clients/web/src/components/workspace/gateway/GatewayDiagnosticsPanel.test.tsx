import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayDiagnostics, GatewayWorkspace } from "../../../types/backend";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import { installGatewayFetch, restoreSessionHookGlobals } from "../../../hooks/session/sessionHookTestFixtures";
import GatewayDiagnosticsPanel from "./GatewayDiagnosticsPanel";
import type { GatewayDiagnosticLog } from "../../../types/backend";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  restoreSessionHookGlobals();
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  restoreGlobalDescriptor("document", originalDocument);
});

const workspace: GatewayWorkspace = {
  workspace_id: "gw_local",
  name: "本地项目",
  root_path: "/workspace/local",
  backend_url: "http://127.0.0.1:8010",
  connection_kind: "local",
  status: "ready",
  active: true,
  managed: true,
  removable: true,
  system_default: true,
  remote: null,
  services: {},
  checked_at: "2026-08-02T00:00:00Z",
};

function secondWorkspace(): GatewayWorkspace {
  return { ...workspace, workspace_id: "gw_second", name: "第二个工作区" };
}

function logFor(label: string): GatewayDiagnosticLog {
  return {
    log_id: `log_${label}`,
    source: "gateway",
    workspace_id: null,
    workspace_name: null,
    service: "gateway",
    label: `日志-${label}`,
    status: "available",
    tail: `TAIL_${label}`,
    truncated: false,
    line_count: 1,
    size_bytes: 10,
    updated_at: "2026-08-02T00:00:00Z",
    error: null,
  } as unknown as GatewayDiagnosticLog;
}

function diagnosticsFor(label: string): GatewayDiagnostics {
  const base = diagnostics();
  return { ...base, gateway_name: label, selected_log_id: `log_${label}`, logs: [logFor(label)] };
}

function diagnostics(): GatewayDiagnostics {
  return {
    gateway_id: "gateway_local",
    gateway_name: "本机 Gateway",
    gateway_connection_id: null,
    connection_kind: "local",
    status: "ready",
    checked_at: "2026-08-02T00:00:00Z",
    selected_workspace_id: null,
    selected_log_id: "log_gateway",
    workspaces: [],
    logs: [
      {
        log_id: "log_gateway",
        source: "gateway",
        workspace_id: null,
        workspace_name: null,
        service: "gateway",
        label: "Gateway 日志",
        status: "available",
        tail: "第一行\n第二行",
        truncated: false,
        line_count: 2,
        size_bytes: 24,
        updated_at: "2026-08-02T00:00:00Z",
        error: null,
      },
    ],
  } as unknown as GatewayDiagnostics;
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** 非安全上下文：navigator.clipboard 缺失，只能靠 document.execCommand 兼容复制。 */
function stubLegacyClipboard(run: () => void): number {
  Reflect.deleteProperty(navigator, "clipboard");
  let execCommandCalls = 0;
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      createElement: () => ({
        value: "",
        style: { position: "", left: "", top: "" },
        setAttribute: () => undefined,
        focus: () => undefined,
        select: () => undefined,
        remove: () => undefined,
      }),
      body: { appendChild: () => undefined },
      execCommand: () => {
        execCommandCalls += 1;
        return true;
      },
    },
  });
  run();
  return execCommandCalls;
}

test("非安全上下文下复制日志仍走兼容复制，而不是直接报剪贴板错误", async () => {
  installGatewayFetch(({ path }) => {
    if (path === "/api/gateway/diagnostics") {
      return Response.json({ data: diagnostics(), request_id: "req_diag" });
    }
    return undefined;
  }, { token: "diag-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<GatewayDiagnosticsPanel apiPort={8021} workspaces={[workspace]} />);
  });
  await flush();

  const copyButton = renderer.root.findAllByType("button").find(
    (button) => Array.isArray(button.children) && button.children.includes("复制"),
  );
  expect(copyButton).toBeDefined();

  let execCommandCalls = 0;
  await act(async () => {
    execCommandCalls = stubLegacyClipboard(() => copyButton!.props.onClick());
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  expect(execCommandCalls).toBe(1);
  expect(JSON.stringify(renderer.toJSON())).not.toContain("Cannot read properties of undefined");
  renderer.unmount();
});

test("先发后到的旧诊断响应不得覆盖切换后的新范围", async () => {
  // 诊断范围（Gateway/工作区/日志入口）可被连续切换，每次切换都会重发请求。
  // 慢的旧请求若在快的旧请求之后返回，必须被丢弃，否则展示态会回退到用户
  // 已经不看的那个范围，出现「选了 B 却显示 A 的日志」。
  const pending: Array<{ label: string; resolve: () => void }> = [];
  const labels = ["first", "second"];
  let callIndex = 0;
  installGatewayFetch(({ path }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    const label = labels[callIndex] ?? `extra${callIndex}`;
    callIndex += 1;
    return new Promise<Response>((resolve) => {
      pending.push({
        label,
        resolve: () =>
          resolve(
            Response.json({ data: diagnosticsFor(label), request_id: `req_${label}` }),
          ),
      });
    });
  }, { token: "race-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayDiagnosticsPanel apiPort={8023} workspaces={[workspace, secondWorkspace()]} />,
    );
  });
  await flush();

  // 第一次请求仍在途时切换工作区，触发第二次请求。
  const workspaceSelect = renderer.root.findAllByType("select")[1];
  act(() => workspaceSelect.props.onChange({ target: { value: "gw_second" } }));
  await flush();
  expect(pending.length).toBe(2);

  // 新范围先返回，旧范围后返回。
  await act(async () => {
    pending[1].resolve();
    await Promise.resolve();
  });
  await act(async () => {
    pending[0].resolve();
    await Promise.resolve();
  });

  const text = JSON.stringify(renderer.toJSON());
  expect(text).toContain("TAIL_second");
  expect(text).not.toContain("TAIL_first");
  renderer.unmount();
});

test("切换工作区后、新范围响应到达前不得继续渲染旧范围日志", async () => {
  // 切换窗口保护（与底部输出面板同型缺陷）：用户从一个工作区切到另一个时，
  // 新范围的诊断请求仍在途。此前 diagnostics 仍是旧范围的快照，selectedLog
  // 回退到 diagnostics.logs[0] 继续渲染旧工作区的 tail，用户会在「已切到 B」
  // 的面板上读到 A 的日志。必须在同一提交内作废旧快照，形成空窗期而非假数据。
  const pending: Array<{ label: string; resolve: () => void }> = [];
  let callIndex = 0;
  installGatewayFetch(({ path }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    const label = callIndex === 0 ? "first" : "second";
    callIndex += 1;
    return new Promise<Response>((resolve) => {
      pending.push({
        label,
        resolve: () =>
          resolve(
            Response.json({ data: diagnosticsFor(label), request_id: `req_${label}` }),
          ),
      });
    });
  }, { token: "switch-window-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayDiagnosticsPanel apiPort={8025} workspaces={[workspace, secondWorkspace()]} />,
    );
  });
  await flush();
  await act(async () => {
    pending[0].resolve();
    await Promise.resolve();
  });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).toContain("TAIL_first");

  // 切到 gw_second：该范围的诊断请求仍在途。
  const workspaceSelect = renderer.root.findAllByType("select")[1];
  act(() => workspaceSelect.props.onChange({ target: { value: "gw_second" } }));
  await flush();
  expect(pending.length).toBe(2);

  const during = JSON.stringify(renderer.toJSON());
  expect(during).not.toContain("TAIL_first");
  expect(during).not.toContain("日志读取失败");

  // 新范围到达后正常渲染，证明上面是切换空窗而不是面板被卡死。
  await act(async () => {
    pending[1].resolve();
    await Promise.resolve();
  });
  await flush();
  const after = JSON.stringify(renderer.toJSON());
  expect(after).toContain("TAIL_second");
  expect(after).not.toContain("TAIL_first");
  renderer.unmount();
});
