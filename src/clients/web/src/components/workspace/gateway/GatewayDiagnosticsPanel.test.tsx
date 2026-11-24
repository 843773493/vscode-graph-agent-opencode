import { afterEach, expect, test } from "bun:test";
import { t } from "../../../../../shared/core/i18n/index.js";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayDiagnostics, GatewayWorkspace } from "../../../types/backend";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import {
  installGatewayFetch,
  restoreGatewayTestEnvironment,
} from "../../../tests/fixtures/session/gatewayEnvironment";
import GatewayDiagnosticsPanel from "./GatewayDiagnosticsPanel";
import type { GatewayDiagnosticLog } from "../../../types/backend";
import { HttpRequestError } from "../../../api/http";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
const elementGlobals = ["HTMLElement", "HTMLInputElement", "HTMLTextAreaElement"] as const;
const originalElementGlobals = elementGlobals.map(name => Object.getOwnPropertyDescriptor(globalThis, name));

afterEach(() => {
  restoreGatewayTestEnvironment();
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  restoreGlobalDescriptor("document", originalDocument);
  elementGlobals.forEach((name, index) => restoreGlobalDescriptor(name, originalElementGlobals[index]));
});

const workspace: GatewayWorkspace = {
  gateway_id: "gateway-test",
    workspace_id: "gw_local",
  name: "本地项目",
  backend_url: "http://127.0.0.1:8010",
  connection_kind: "local",
  status: "ready",
  active: true,
  managed: true,
  affected_workspace_ids: [],
  removable: true,
  system_default: true,
  remote_gateways: [],
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
    processes: [],
    plugin_observations: [],
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
  for (const name of elementGlobals) Object.defineProperty(globalThis, name, { configurable: true, value: class {} });
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

test("非安全上下文下复制诊断摘要仍走原剪贴板入口", async () => {
  installGatewayFetch(({ path }) => {
    if (path === "/api/gateway/diagnostics") {
      return Response.json({ data: diagnostics(), request_id: "req_diag" });
    }
    return undefined;
  }, { token: "diag-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8021} />);
  });
  await flush();

  const copyButton = renderer.root.findAllByType("button").find(
    (button) => Array.isArray(button.children) && button.children.includes("复制诊断摘要"),
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

test("展示实际日志进程、stdout-only 与逐进程失败", async () => {
  const snapshot = diagnostics();
  snapshot.processes = [
    { service: "gateway", workspace_ids: [], output_mode: "stdout-only", error: null,
      state: { service: "gateway", pid: 123, runtime_instance_id: "gateway-instance", applied_revision: "actual-revision",
        level: "warning", pretty: false, log_sink: null } },
    { service: "backend", workspace_ids: [workspace.workspace_id, "another-workspace"], output_mode: "file", error: null,
      state: { service: "backend", pid: 456, runtime_instance_id: "backend-instance", applied_revision: "actual-revision",
        level: "warning", pretty: true, log_sink: { log_id: "backend-log", owner_id: "actual-owner", role: "backend",
          source: "gateway", device: "1", inode: "2" } } },
    { service: "browser", workspace_ids: [workspace.workspace_id], output_mode: "unknown", state: null,
      error: "日志状态读取失败: browser" },
  ];
  installGatewayFetch(({ path }) => path === "/api/gateway/diagnostics"
    ? Response.json({ data: snapshot, request_id: "req_processes" }) : undefined, { token: "diag-token" });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8021} />); });
  await flush();
  const output = JSON.stringify(renderer.toJSON());
  for (const value of ["gateway-instance", "backend-instance", "actual-revision", "stdout-only", "backend-log",
    "actual-owner", "another-workspace", "日志状态读取失败: browser", "WARNING"]) {
    expect(output).toContain(value);
  }
  expect(renderer.root.findAllByProps({ role: "alert" }).some((node) => node.children.includes("日志状态读取失败: browser"))).toBe(true);
  renderer.unmount();
});

test("诊断摘要不复制日志、错误正文或未知扩展字段", async () => {
  const snapshot = diagnostics();
  snapshot.logs[0].tail = "model-body-secret";
  snapshot.logs[0].error = "error-with-api-key";
  snapshot.control_directory = "/target/control-directory";
  snapshot.log_directory = "/target/log-directory";
  const response = { ...snapshot, api_key: "must-not-copy" };
  installGatewayFetch(({ path }) => path === "/api/gateway/diagnostics"
    ? Response.json({ data: response, request_id: "req_safe_copy" }) : undefined, { token: "diag-token" });
  let copied = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (text: string) => { copied = text; } } });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8021} />); });
  await flush();
  const copy = renderer.root.findAllByType("button").find(button => button.children.includes("复制诊断摘要"));
  expect(copy).toBeDefined();
  await act(async () => { copy!.props.onClick(); await Promise.resolve(); });
  const summary = JSON.parse(copied);
  expect(summary.gateway_id).toBe(snapshot.gateway_id);
  expect(summary.logs[0]).toMatchObject({ log_id: "log_gateway", has_error: true });
  for (const secret of ["model-body-secret", "error-with-api-key", "must-not-copy", "diag-token", "/target/control-directory", "/target/log-directory"]) expect(copied).not.toContain(secret);
  renderer.unmount();
});

test("存储位置只展示原诊断归属，设置失败显示简短原因", async () => {
  const snapshot = { ...diagnostics(), control_directory: String.raw`C:\BoxTeam\control`, log_directory: String.raw`C:\BoxTeam\logs` };
  const targets: unknown[] = [];
  installGatewayFetch(({ path }) => path === "/api/gateway/diagnostics"
    ? Response.json({ data: snapshot, request_id: "req_storage" }) : undefined, { token: "diag-token" });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8035}
    onOpenRetention={async target => {
      targets.push(target);
      throw new HttpRequestError(403, "Forbidden", "configuration_read", "/api/gateway/config/sources", "req_retention_denied");
    }} />); });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).toContain(JSON.stringify(snapshot.control_directory).slice(1, -1));
  const open = renderer.root.findAllByType("button").find(button => button.children.includes("打开 Gateway 日志保留设置"));
  await act(async () => { open!.props.onClick(); await Promise.resolve(); });
  expect(targets).toEqual([{ gateway_id: snapshot.gateway_id, gateway_connection_id: undefined }]);
  expect(JSON.stringify(renderer.toJSON())).toContain("configuration_read");
  expect(JSON.stringify(renderer.toJSON())).not.toContain("req_retention_denied");
  let copied = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (text: string) => { copied = text; } } });
  const copy = renderer.root.findAllByType("button").find(button => button.children.includes(t('shell.copyDiagnostic')));
  expect(copy).toBeDefined();
  await act(async () => { copy!.props.onClick(); await Promise.resolve(); });
  expect(copied).toContain("req_retention_denied");
  expect(copied).toContain("/api/gateway/config/sources");
  expect(copied).toContain("configuration_read");
  renderer.unmount();
});

test("运行统计消费原诊断回执并区分未知与真实零值", async () => {
  const snapshot = diagnostics();
  snapshot.observation = { instance_id: "observed-gateway", applied_revision: "rev-observed",
    telemetry: { enabled: false, sample_ratio: 0.5, sample_buffer_limit: 256 },
    statistics: { enabled: true, collection_interval_ms: 5000 },
    requests: { count: 7, errors: 1, duration_ms: 42, proxy_count: 2, proxy_errors: 0, proxy_duration_ms: 11 },
    samples: [], processes: [{ service: "gateway", pid: 123, rss_bytes: 0, cpu_seconds: 0, error: null },
      { service: "worker", pid: 124, rss_bytes: null, cpu_seconds: null, error: "指标读取失败" }],
    collection_error: null, unsupported_metrics: ["gpu"] };
  let calls = 0;
  installGatewayFetch(({ path }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    calls += 1;
    return Response.json({ data: snapshot, request_id: "req_observation" });
  }, { token: "diag-token" });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8021} />); });
  await flush();
  const output = JSON.stringify(renderer.toJSON());
  for (const value of ["observed-gateway", "rev-observed", "遥测：已停用", "运行统计：已启用", "HTTP 请求：7", "累计耗时 42 ms",
    "其中代理请求：2", "CPU 时间 0 秒", "CPU 时间 未知 秒", "指标读取失败", "gpu"]) expect(output).toContain(value);
  expect(calls).toBe(1);
  renderer.unmount();
});

test("相同工作区 UUID 切换 Gateway，旧诊断响应不得覆盖新范围", async () => {
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
      <GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8023} target={{ gateway_id: "gateway-first", workspace_id: workspace.workspace_id }} />,
    );
  });
  await flush();

  // 保持工作区 UUID 不变，只切换稳定 Gateway 身份，也必须发起新请求。
  act(() => renderer.update(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8023}
    target={{ gateway_id: "gateway-second", workspace_id: workspace.workspace_id }} />));
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
  expect(text).toContain("日志-second");
  expect(text).not.toContain("日志-first");
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
      <GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8025} />,
    );
  });
  await flush();
  await act(async () => {
    pending[0].resolve();
    await Promise.resolve();
  });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).toContain("日志-first");

  // 切到 gw_second：该范围的诊断请求仍在途。
  act(() => renderer.update(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8025}
    target={secondWorkspace()} />));
  await flush();
  expect(pending.length).toBe(2);

  const during = JSON.stringify(renderer.toJSON());
  expect(during).not.toContain("日志-first");
  expect(during).not.toContain("日志读取失败");

  // 新范围到达后正常渲染，证明上面是切换空窗而不是面板被卡死。
  await act(async () => {
    pending[1].resolve();
    await Promise.resolve();
  });
  await flush();
  const after = JSON.stringify(renderer.toJSON());
  expect(after).toContain("日志-second");
  expect(after).not.toContain("日志-first");
  renderer.unmount();
});

test("刷新失败保留上次诊断快照并使用固定反馈槽", async () => {
  let requestCount = 0;
  installGatewayFetch(({ path }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    requestCount += 1;
    if (requestCount === 1) {
      return Response.json({ data: diagnosticsFor("gateway"), request_id: "req_diag_initial" });
    }
    return Response.json(
      { detail: "Gateway 诊断服务暂不可用" },
      { status: 503, headers: { "content-type": "application/json" } },
    );
  }, { token: "diag-refresh-failure-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8027} />);
  });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).toContain("日志-gateway");
  expect(renderer.root.findByProps({ className: "gateway-console-feedback-slot" }).children).toHaveLength(0);

  const refreshButton = renderer.root.findAllByType("button").find((button) => (
    button.children.some((child) => child === "刷新日志")
  ));
  expect(refreshButton).toBeDefined();
  await act(async () => {
    refreshButton!.props.onClick();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  const rendered = JSON.stringify(renderer.toJSON());
  expect(rendered).toContain("日志-gateway");
  expect(rendered).toContain("刷新失败");
  expect(rendered).toContain("Gateway 诊断服务暂不可用");
  expect(rendered).not.toContain("正在读取 Gateway 诊断信息");
  expect(renderer.root.findAllByProps({ role: "alert" })).toHaveLength(1);
  expect(renderer.root.findByProps({ className: "gateway-console-feedback-slot" })).toBeDefined();
  renderer.unmount();
});

test("首次诊断读取失败展示空错误态并停止加载提示", async () => {
  installGatewayFetch(({ path }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    return Response.json(
      { detail: "Gateway 诊断服务暂不可用" },
      { status: 503, headers: { "content-type": "application/json", "X-Request-ID": "req_failed_diagnostics" } },
    );
  }, { token: "diag-initial-failure-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8029} />);
  });
  await act(async () => {
    await flush();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  const rendered = JSON.stringify(renderer.toJSON());
  expect(rendered).toContain("日志读取失败");
  expect(rendered).toContain("当前范围没有可用的诊断快照");
  expect(rendered).toContain("Gateway 诊断服务暂不可用");
  expect(rendered).not.toContain("req_failed_diagnostics");
  expect(rendered).not.toContain("正在读取 Gateway 诊断信息");
  expect(renderer.root.findByProps({ className: "gateway-console-feedback-slot" })).toBeDefined();
  renderer.unmount();
});

test("切换诊断范围及关闭后取消原读取，不发布迟到快照", async () => {
  const requests: Array<{ signal: AbortSignal; resolve: (response: Response) => void }> = [];
  installGatewayFetch(({ path, init }) => {
    if (path !== "/api/gateway/diagnostics") return undefined;
    return new Promise<Response>(resolve => requests.push({ signal: init!.signal!, resolve }));
  }, { token: "diag-cancel-token" });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8033} />); });
  await flush();
  expect(requests).toHaveLength(1);
  act(() => renderer.update(<GatewayDiagnosticsPanel onOpenLog={async () => {}} apiPort={8033}
    target={secondWorkspace()} />));
  await flush();
  expect(requests).toHaveLength(2);
  expect(requests[0]!.signal.aborted).toBe(true);
  requests[0]!.resolve(Response.json({ data: diagnosticsFor("stale"), request_id: "req_stale" }));
  await flush();
  expect(JSON.stringify(renderer.toJSON())).not.toContain("日志-stale");
  act(() => renderer.unmount());
  expect(requests[1]!.signal.aborted).toBe(true);
  requests[1]!.resolve(Response.json({ data: diagnosticsFor("closed"), request_id: "req_closed" }));
  await flush();
});


test.each(["gateway", "workspace"] as const)("日志交原生输出，只为工作区日志携带工作区归属：%s", async source => {
  const snapshot = diagnostics();
  snapshot.gateway_connection_id = "rgw_remote";
  snapshot.selected_workspace_id = "workspace-remote";
  snapshot.logs[0].source = source;
  if (source === "workspace") snapshot.logs[0].workspace_id = "workspace-log-owner";
  installGatewayFetch(({ path }) => path === "/api/gateway/diagnostics"
    ? Response.json({ data: snapshot, request_id: "req_output" }) : undefined, { token: "diag-output-token" });
  const opened: unknown[] = [];
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticsPanel apiPort={8021} onOpenLog={async target => { opened.push(target); }} />); });
  await flush();
  try {
    expect(renderer.root.findAllByType("pre")).toHaveLength(0);
    const button = renderer.root.findAllByType("button").find(button => button.children.includes("在输出中查看日志"));
    expect(button).toBeDefined();
    await act(async () => { button!.props.onClick(); await Promise.resolve(); });
    expect(opened).toEqual([{ gateway_id: snapshot.gateway_id, gateway_connection_id: "rgw_remote",
      workspace_id: source === "workspace" ? "workspace-log-owner" : undefined, log_id: snapshot.selected_log_id }]);
  } finally { await act(async () => renderer.unmount()); }
});
