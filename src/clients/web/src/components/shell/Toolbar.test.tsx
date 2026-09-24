import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import { AppContext, type AppContextType } from "../../hooks";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import Toolbar from "./Toolbar";

/**
 * 工具栏「更新」按钮的诚实性契约。
 *
 * 本地运行时既没有「检查更新 / 拉取新版本」接口，也没有把当前构建版本暴露给前端。
 * 此前该按钮点击后只写死一条宣称「已是最新」的状态：不发任何请求、不读任何版本，
 * 等于凭空宣称一次成功的更新检查（真实浏览器审查判为假成功）。本文件锁住修复后的
 * 形态——按钮为禁用态并如实说明，且源码里不再保留那条伪造成功的状态写入。
 */

const appContextValue = {
  state: { apiPort: 8014, gatewayUserAccess: null, status: null } as unknown as AppState,
  setStatus: () => {},
} as unknown as AppContextType;

const BASE_PROPS: Parameters<typeof Toolbar>[0] = {
  sessionTitle: "会话",
  onCreateSession: () => {},
  auxiliaryVisible: false,
  onToggleAuxiliaryPanel: () => {},
  chatVisible: false,
  onToggleChatPanel: () => {},
  agentSessionsVisible: false,
  onToggleAgentSessionsPanel: () => {},
  panelVisible: false,
  onTogglePanel: () => {},
  workbenchView: "sessions",
  onWorkbenchViewChange: () => {},
  showAuxiliaryToggle: true,
};

// @floating-ui 用 `instanceof window.Element` 判定引用（Toolbar 内嵌的
// GatewayUserAccessMenu 会走 AnchoredOverlay），测试环境没有这些全局，补最小构造器。
class OverlayElementStub {}
class OverlayNodeStub {}
const OVERLAY_CONSTRUCTORS = {
  Element: OverlayElementStub,
  Node: OverlayNodeStub,
  HTMLElement: OverlayElementStub,
};
const OVERLAY_CONSTRUCTOR_NAMES = ["Element", "Node", "HTMLElement"] as const;

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const mountedRenderers: ReactTestRenderer[] = [];

function installWindow(): void {
  for (const [name, value] of Object.entries(OVERLAY_CONSTRUCTORS)) {
    Object.defineProperty(globalThis, name, { configurable: true, value });
  }
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      ...OVERLAY_CONSTRUCTORS,
      location: { port: "8014", origin: "http://127.0.0.1:8011" },
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const name of OVERLAY_CONSTRUCTOR_NAMES) {
    Reflect.deleteProperty(globalThis, name);
  }
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

function renderToolbarWithContext(): ReactTestRenderer {
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(
      <AppContext.Provider value={appContextValue}>
        <Toolbar {...BASE_PROPS} />
      </AppContext.Provider>,
    );
  });
  mountedRenderers.push(renderer!);
  return renderer!;
}

describe("Toolbar 更新按钮诚实性契约", () => {
  test("更新按钮为禁用态并如实说明，不再宣称已是最新", () => {
    installWindow();
    const renderer = renderToolbarWithContext();
    const update = renderer.root.find(
      (node) => node.type === "button" && node.props?.["aria-label"] === "更新",
    );
    expect(update.props.disabled).toBe(true);
    expect(update.props.title).toBe("Web 端暂无更新检查");
    expect(update.props.title).not.toContain("最新");
    expect(update.props.title).not.toContain("已是当前本地构建");
  });

  test("更新按钮没有任何点击回调，无法伪造一次成功的更新检查", () => {
    installWindow();
    const renderer = renderToolbarWithContext();
    const update = renderer.root.find(
      (node) => node.type === "button" && node.props?.["aria-label"] === "更新",
    );
    expect(update.props.onClick).toBeUndefined();
  });

  test("Toolbar 源码不再依赖 setStatus 制造虚假更新成功", async () => {
    const source = await Bun.file(new URL("./Toolbar.tsx", import.meta.url)).text();
    expect(source).not.toContain("已是当前本地构建");
    expect(source).not.toContain("setStatus");
    expect(source).not.toContain("useAppState");
  });
});
