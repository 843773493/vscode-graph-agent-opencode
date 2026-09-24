import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayDiagnostics, GatewayWorkspace } from "../../../types/backend";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import { installGatewayFetch, restoreSessionHookGlobals } from "../../../hooks/session/sessionHookTestFixtures";
import GatewayDiagnosticsPanel from "./GatewayDiagnosticsPanel";

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
