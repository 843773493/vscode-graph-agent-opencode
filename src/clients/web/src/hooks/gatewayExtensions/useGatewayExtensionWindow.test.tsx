import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WorkspaceAuxiliaryTab } from "../../components/workspace/WorkspaceAuxiliaryPanel";
import type { WorkspaceRuntimePreviewTab } from "../../components/workspace/WorkspaceRuntimePreviewArea";
import type { GatewayExtensionResourceEntry } from "./useGatewayExtensionResources";
import type { SessionResource } from "../../types/backend";
import { useGatewayExtensionWindow } from "./useGatewayExtensionWindow";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalFetch = globalThis.fetch;

// createSessionConnection 的真实实现会经 http.ts 的认证屏障发起网络请求；这里改用
// fetch 桩记录实际转发的工作区、会话与资源类型。不要再用 mock.module：bun 的模块
// 注册表是进程级的，mock.restore() 无法跨文件撤销，会污染随后加载同一模块的测试。
const createSessionConnectionCalls: Array<[number, string, string, string]> = [];
let createdConnectionResourceId = "created-browser-1";

function installCreateSessionConnectionStub(port: number): void {
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return Response.json({ code: 0, message: "ok", request_id: "req_token", data: { token: "test-token" } });
      }
      const match = /\/api\/gateway\/workspaces\/([^/]+)\/([^/]+)\/api\/(browsers|terminals)/.exec(url);
      if (match) {
        const workspaceId = decodeURIComponent(match[1]);
        const service = match[2];
        const kind = service === "browser-manager" ? "browser" : "terminal";
        const payload = JSON.parse(String(init?.body)) as { session_id: string };
        createSessionConnectionCalls.push([port, workspaceId, payload.session_id, kind]);
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_create",
          data: kind === "browser" ? { browser_id: createdConnectionResourceId } : { terminal_id: createdConnectionResourceId },
        });
      }
      throw new Error("未预期的请求: " + url);
    },
    { preconnect: originalFetch.preconnect },
  );
}

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");

type StubWindow = {
  location: {
    origin: string;
    href: string;
    pathname: string;
    search: string;
    hash: string;
    assign: (url: string) => void;
  };
  open: (url: string, name: string) => unknown;
  close: () => void;
  opener: { closed: boolean } | null;
};

type Mounted = ReturnType<typeof useGatewayExtensionWindow>;

function entry(
  kind: "browser" | "terminal",
  resourceId: string,
  status = "running",
): GatewayExtensionResourceEntry {
  return {
    key: `w1:s1:${kind}:${resourceId}`,
    gateway_name: "本地网关",
    workspace_name: "工作区",
    session_title: "会话",
    workspace_id: "w1",
    session_id: "s1",
    connection_kind: "local",
    resource: {
      resource_id: resourceId,
      session_id: "s1",
      kind,
      name: `${kind}-${resourceId}`,
      status,
    },
  } as unknown as GatewayExtensionResourceEntry;
}

function sessionResource(
  kind: "browser" | "terminal",
  resourceId: string,
  status: string,
): SessionResource {
  return {
    resource_id: resourceId,
    session_id: "s1",
    kind,
    name: `${kind}-${resourceId}`,
    status,
    created_at: "2026-09-21T00:00:00Z",
    updated_at: "2026-09-21T00:00:00Z",
    available_actions: ["cancel"],
    metadata: {},
  };
}

interface MountOptions {
  extensionWindowRequested?: boolean;
  extensionWindowFallback?: boolean;
  auxiliaryTab?: WorkspaceAuxiliaryTab;
  sharedPreviewVisible?: boolean;
  activeRuntimePreview?: WorkspaceRuntimePreviewTab | null;
  sessionResources?: SessionResource[];
  entries?: GatewayExtensionResourceEntry[];
  selectedEntry?: GatewayExtensionResourceEntry | null;
  openResult?: unknown;
}

let renderers: ReactTestRenderer[] = [];

async function mountHook(options: MountOptions = {}) {
  installCreateSessionConnectionStub(49_507);
  const selects: (string | null)[] = [];
  const auxiliaryTabs: WorkspaceAuxiliaryTab[] = [];
  const statuses: string[] = [];
  const fallbackWrites: boolean[] = [];
  const browserPreviews: string[] = [];
  const terminalPreviews: string[] = [];
  let refreshes = 0;
  let hook: Mounted | undefined;

  const extensionResources = {
    entries: options.entries ?? [],
    errors: [],
    loading: false,
    loadedAt: null,
    selectedKey: null,
    selectedEntry: options.selectedEntry ?? null,
    select: (key: string | null) => {
      selects.push(key);
    },
    refresh: async () => {
      refreshes += 1;
    },
    control: async () => {},
  };

  function Probe(): React.ReactNode {
    hook = useGatewayExtensionWindow({
      extensionWindowRequested: options.extensionWindowRequested ?? false,
      extensionWindowFallback: options.extensionWindowFallback ?? false,
      setExtensionWindowFallback: (update) => {
        fallbackWrites.push(typeof update === "function" ? update(false) : update);
      },
      auxiliaryTab: options.auxiliaryTab ?? "files",
      sharedPreviewVisible: options.sharedPreviewVisible ?? false,
      activeRuntimePreview: options.activeRuntimePreview ?? null,
      sessionResources: options.sessionResources ?? [],
      extensionResources: extensionResources as never,
      openAuxiliaryTab: (tab) => {
        auxiliaryTabs.push(tab);
      },
      openBrowserPreview: (id) => {
        browserPreviews.push(id);
      },
      openTerminalPreview: (id) => {
        terminalPreviews.push(id);
      },
      apiPort: 49_507,
      activeSessionWorkspaceId: "w1",
      activeSessionId: "s1",
      setStatus: (text) => {
        statuses.push(text);
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  renderers.push(renderer!);
  return {
    hook: hook!,
    selects,
    auxiliaryTabs,
    statuses,
    fallbackWrites,
    browserPreviews,
    terminalPreviews,
    get refreshes() {
      return refreshes;
    },
  };
}

function stubWindow(overrides: Partial<StubWindow> = {}): StubWindow {
  const stub: StubWindow = {
    location: {
      origin: "http://127.0.0.1:8011",
      href: "http://127.0.0.1:8011/extension?resourceType=browser",
      pathname: "/extension",
      search: "?resourceType=browser",
      hash: "",
      assign: () => {},
    },
    open: () => ({ focus: () => {} }),
    close: () => {},
    opener: null,
    ...overrides,
  };
  Object.defineProperty(globalThis, "window", { configurable: true, value: stub });
  return stub;
}

afterEach(() => {
  for (const renderer of renderers.splice(0)) {
    act(() => renderer.unmount());
  }
  createSessionConnectionCalls.length = 0;
  createdConnectionResourceId = "created-browser-1";
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindow);
});

describe("useGatewayExtensionWindow 打开扩展窗口", () => {
  test("扩展窗口模式下 kind=debug 只切到 debug 标签，不调用 window.open", async () => {
    let opened = 0;
    stubWindow({ open: () => { opened += 1; return null; } });
    const mounted = await mountHook({ extensionWindowRequested: true });

    act(() => mounted.hook.openExtensionWindow("debug"));

    expect(mounted.auxiliaryTabs).toEqual(["debug"]);
    expect(opened).toBe(0);
    expect(mounted.selects).toEqual([]);
  });

  test("扩展窗口模式下命中已有 entry 时 select(entry.key)，不调用 window.open", async () => {
    let opened = 0;
    stubWindow({ open: () => { opened += 1; return null; } });
    const target = entry("browser", "b9");
    const mounted = await mountHook({
      extensionWindowRequested: true,
      entries: [target],
    });

    act(() => mounted.hook.openExtensionWindow("browser", "b9"));

    expect(mounted.selects).toEqual([target.key]);
    expect(opened).toBe(0);
  });

  test("非扩展窗口模式下调用 window.open，URL 含预期 resourceType 与 resourceId", async () => {
    const openedUrls: string[] = [];
    stubWindow({
      open: (url) => {
        openedUrls.push(url);
        return { focus: () => {} };
      },
    });
    const mounted = await mountHook();

    act(() => mounted.hook.openExtensionWindow("terminal", "t1"));

    expect(openedUrls).toHaveLength(1);
    const url = new URL(openedUrls[0]);
    expect(url.pathname).toBe("/extension");
    expect(url.searchParams.get("resourceType")).toBe("terminal");
    expect(url.searchParams.get("resourceId")).toBe("t1");
    expect(url.searchParams.get("workspaceId")).toBe("w1");
    expect(url.searchParams.get("sessionId")).toBe("s1");
    expect(mounted.statuses).toEqual(["已打开扩展窗口；后续扩展内容将在此窗口内切换。"]);
    expect(mounted.fallbackWrites).toEqual([]);
  });

  test("window.open 返回 falsy 时进入 fallback：置 fallback、打开预览并报错", async () => {
    stubWindow({ open: () => null });
    const mounted = await mountHook();

    act(() => mounted.hook.openExtensionWindow("browser", "b9"));

    expect(mounted.fallbackWrites).toEqual([true]);
    expect(mounted.auxiliaryTabs).toEqual(["resources"]);
    expect(mounted.browserPreviews).toEqual(["b9"]);
    expect(mounted.terminalPreviews).toEqual([]);
    expect(mounted.statuses).toEqual([
      "扩展窗口未能打开，已在当前页面切换为扩展窗口模式；请检查浏览器弹窗权限。",
    ]);
  });

  test("window.open 返回 falsy 且 kind=debug 时切到 debug 标签且不打开预览", async () => {
    stubWindow({ open: () => null });
    const mounted = await mountHook();

    act(() => mounted.hook.openExtensionWindow("debug"));

    expect(mounted.auxiliaryTabs).toEqual(["debug"]);
    expect(mounted.browserPreviews).toEqual([]);
    expect(mounted.terminalPreviews).toEqual([]);
  });
});

describe("useGatewayExtensionWindow 退出扩展窗口", () => {
  test("opener 可用时调用 window.close()", async () => {
    let closed = 0;
    let assigned: string | null = null;
    stubWindow({
      opener: { closed: false },
      close: () => { closed += 1; },
      location: {
        origin: "http://127.0.0.1:8011",
        href: "http://127.0.0.1:8011/extension?resourceType=browser",
        pathname: "/extension",
        search: "?resourceType=browser",
        hash: "",
        assign: (url: string) => { assigned = url; },
      },
    });
    const mounted = await mountHook({ extensionWindowRequested: true });

    act(() => mounted.hook.handleExitExtensionWindow());

    expect(closed).toBe(1);
    expect(assigned).toBeNull();
  });

  test("opener 不可用时 location.assign 且 pathname 重置为 /", async () => {
    let assigned: string | null = null;
    let closed = 0;
    stubWindow({
      opener: { closed: true },
      close: () => { closed += 1; },
      location: {
        origin: "http://127.0.0.1:8011",
        href: "http://127.0.0.1:8011/extension?resourceType=browser#frag",
        pathname: "/extension",
        search: "?resourceType=browser",
        hash: "#frag",
        assign: (url: string) => { assigned = url; },
      },
    });
    const mounted = await mountHook({ extensionWindowRequested: true });

    act(() => mounted.hook.handleExitExtensionWindow());

    expect(closed).toBe(0);
    const target = new URL(assigned!);
    expect(target.pathname).toBe("/");
    expect(target.search).toBe("");
    expect(target.hash).toBe("");
  });

  test("非扩展窗口模式只清除 fallback", async () => {
    let closed = 0;
    stubWindow({ close: () => { closed += 1; } });
    const mounted = await mountHook({ extensionWindowRequested: false });

    act(() => mounted.hook.handleExitExtensionWindow());

    expect(mounted.fallbackWrites).toEqual([false]);
    expect(closed).toBe(0);
  });
});

describe("useGatewayExtensionWindow 预览标签选择", () => {
  test("扩展窗口模式下 runtimePreviewTab 取自扩展资源选择", async () => {
    stubWindow();
    const mounted = await mountHook({
      extensionWindowRequested: true,
      extensionWindowFallback: false,
      selectedEntry: entry("browser", "b9"),
    });

    expect(mounted.hook.runtimePreviewTab?.previewType).toBe("browser");
    expect(mounted.hook.runtimePreviewTab?.path).toBe("gateway-resource://w1:s1:browser:b9");
  });

  test("browser 标签的 name、scopeLabel 与 attachUrl 取自扩展 entry 契约", async () => {
    stubWindow();
    const selected = entry("browser", "b9");
    const mounted = await mountHook({
      extensionWindowRequested: true,
      selectedEntry: selected,
    });

    const tab = mounted.hook.runtimePreviewTab!;
    expect(tab.name).toBe("browser-b9");
    expect(tab.scopeLabel).toBe(
      `${selected.gateway_name} · ${selected.workspace_name} · ${selected.session_title}`,
    );
    expect(tab.attachUrl).toBe(
      "http://127.0.0.1:8011/api/gateway/attach/browser/?" +
        `workspaceId=${selected.workspace_id}&browserId=${selected.resource.resource_id}&embedded=1`,
    );
  });

  test("terminal 标签的 name、scopeLabel 与 attachUrl 取自扩展 entry 契约", async () => {
    stubWindow();
    const selected = entry("terminal", "t7");
    const mounted = await mountHook({
      extensionWindowRequested: true,
      selectedEntry: selected,
    });

    const tab = mounted.hook.runtimePreviewTab!;
    expect(tab.name).toBe("terminal-t7");
    expect(tab.scopeLabel).toBe(
      `${selected.gateway_name} · ${selected.workspace_name} · ${selected.session_title}`,
    );
    expect(tab.attachUrl).toBe(
      "http://127.0.0.1:8011/api/gateway/attach/terminal/?" +
        `workspaceId=${selected.workspace_id}&terminalId=${selected.resource.resource_id}&embedded=1`,
    );
  });

  test("扩展窗口模式下 kind 不在 browser/terminal 白名单内的选择不产生预览标签", async () => {
    stubWindow();
    const debugEntry = {
      ...entry("browser", "b9"),
      resource: { ...entry("browser", "b9").resource, kind: "debug" },
    } as unknown as GatewayExtensionResourceEntry;
    const mounted = await mountHook({
      extensionWindowRequested: true,
      selectedEntry: debugEntry,
    });

    expect(mounted.hook.runtimePreviewTab).toBeNull();
  });

  test("fallback 且活动运行资源命中时 runtimePreviewTab 取自活动预览", async () => {
    stubWindow();
    const active: WorkspaceRuntimePreviewTab = {
      previewType: "terminal",
      path: "terminal://t1",
      name: "终端 t1",
      terminalId: "t1",
      attachUrl: "http://127.0.0.1:8011/api/gateway/attach/terminal/?terminalId=t1",
    };
    const mounted = await mountHook({
      extensionWindowRequested: false,
      extensionWindowFallback: true,
      activeRuntimePreview: active,
      sessionResources: [sessionResource("terminal", "t1", "running")],
    });

    expect(mounted.hook.runtimePreviewTab?.path).toBe("terminal://t1");
    expect(mounted.hook.extensionWindowVisible).toBe(true);
  });

  test("fallback 但活动资源非 running 时 runtimePreviewTab 为 null", async () => {
    stubWindow();
    const active: WorkspaceRuntimePreviewTab = {
      previewType: "terminal",
      path: "terminal://t1",
      name: "终端 t1",
      terminalId: "t1",
      attachUrl: "http://127.0.0.1:8011/api/gateway/attach/terminal/?terminalId=t1",
    };
    const mounted = await mountHook({
      extensionWindowRequested: false,
      extensionWindowFallback: true,
      activeRuntimePreview: active,
      sessionResources: [sessionResource("terminal", "t1", "stopped")],
    });

    expect(mounted.hook.runtimePreviewTab).toBeNull();
  });

  test("扩展窗口请求与 fallback 同时成立时优先取扩展资源标签", async () => {
    stubWindow();
    const active: WorkspaceRuntimePreviewTab = {
      previewType: "terminal",
      path: "terminal://t1",
      name: "终端 t1",
      terminalId: "t1",
      attachUrl: "http://127.0.0.1:8011/api/gateway/attach/terminal/?terminalId=t1",
    };
    const mounted = await mountHook({
      extensionWindowRequested: true,
      extensionWindowFallback: true,
      activeRuntimePreview: active,
      selectedEntry: entry("browser", "b9"),
      sessionResources: [sessionResource("terminal", "t1", "running")],
    });

    expect(mounted.hook.runtimePreviewTab?.path).toBe("gateway-resource://w1:s1:browser:b9");
  });

  test("extensionDebugSplitActive 需要可见、debug 标签与共享预览同时成立", async () => {
    stubWindow();
    const splitOn = await mountHook({
      extensionWindowRequested: true,
      auxiliaryTab: "debug",
      sharedPreviewVisible: true,
    });
    expect(splitOn.hook.extensionDebugSplitActive).toBe(true);

    const splitOff = await mountHook({
      extensionWindowRequested: true,
      auxiliaryTab: "debug",
      sharedPreviewVisible: false,
    });
    expect(splitOff.hook.extensionDebugSplitActive).toBe(false);
  });

  test("fallback 但资源 id 相同而 kind 不同时 runtimePreviewTab 为 null", async () => {
    stubWindow();
    const active: WorkspaceRuntimePreviewTab = {
      previewType: "browser",
      path: "browser://r1",
      name: "浏览器 r1",
      browserId: "r1",
      attachUrl: "http://127.0.0.1:8011/api/gateway/attach/browser/?browserId=r1",
    };
    const mounted = await mountHook({
      extensionWindowRequested: false,
      extensionWindowFallback: true,
      activeRuntimePreview: active,
      sessionResources: [sessionResource("terminal", "r1", "running")],
    });

    expect(mounted.hook.runtimePreviewTab).toBeNull();
  });
});

describe("useGatewayExtensionWindow 扩展资源动作", () => {
  test("openExtensionResource 原样转发 entry.key 并输出完整范围文案", async () => {
    stubWindow();
    const target = entry("browser", "b9");
    const mounted = await mountHook({ selectedEntry: target });

    act(() => mounted.hook.openExtensionResource(target));

    expect(mounted.selects).toEqual([target.key]);
    expect(mounted.statuses).toEqual([
      `已切换到 ${target.gateway_name} · ${target.workspace_name} · ${target.session_title}`,
    ]);
  });

  test("createExtensionReplacement 以 browser kind 新建连接、刷新列表并报告资源标识", async () => {
    stubWindow();
    createdConnectionResourceId = "created-browser-42";
    const target = entry("browser", "b9");
    const mounted = await mountHook({ selectedEntry: target });

    await act(async () => {
      await mounted.hook.createExtensionReplacement(target);
    });

    expect(createSessionConnectionCalls).toEqual([
      [49_507, target.workspace_id, target.session_id, "browser"],
    ]);
    expect(mounted.refreshes).toBe(1);
    expect(mounted.statuses).toEqual(["已新建浏览器：created-browser-42"]);
  });
});

describe("useGatewayExtensionWindow 扩展窗口目标匹配", () => {
  test("kind 相同但 resource_id 不同、resource_id 相同但 kind 不同都不触发 select", async () => {
    stubWindow();
    const mounted = await mountHook({
      extensionWindowRequested: true,
      entries: [entry("browser", "b1"), entry("terminal", "b9")],
    });

    act(() => mounted.hook.openExtensionWindow("browser", "b9"));

    expect(mounted.selects).toEqual([]);
  });
});
