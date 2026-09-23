import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as storage from "../../state/storage";
import type { AppState } from "../../types/frontend";
import { useUnreadSessionTracking } from "./useUnreadSessionTracking";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const SESSION_CACHE_KEY = "workspace-unread::session-unread";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

interface DocStub {
  visibilityState: string;
  hasFocus: () => boolean;
  addEventListener: (type: string, listener: () => void) => void;
  removeEventListener: (type: string, listener: () => void) => void;
  fire: (type: string) => void;
}

function installDoc(visible: boolean, focused: boolean): DocStub {
  const listeners = new Map<string, Set<() => void>>();
  const doc: DocStub = {
    visibilityState: visible ? "visible" : "hidden",
    hasFocus: () => focused,
    addEventListener: (type, listener) => {
      const set = listeners.get(type) ?? new Set();
      set.add(listener);
      listeners.set(type, set);
    },
    removeEventListener: (type, listener) => {
      listeners.get(type)?.delete(listener);
    },
    fire: (type) => {
      for (const listener of listeners.get(type) ?? []) listener();
    },
  };
  Object.defineProperty(globalThis, "document", { configurable: true, value: doc });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      addEventListener: (type: string, listener: () => void) => {
        doc.addEventListener(type, listener);
      },
      removeEventListener: (type: string, listener: () => void) => {
        doc.removeEventListener(type, listener);
      },
    },
  });
  return doc;
}

function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    unreadSessionKeys: new Set<string>(),
    ...overrides,
  } as unknown as AppState;
}

let renderer: ReactTestRenderer | undefined;
const restores: Array<() => void> = [];

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  for (const restore of restores.splice(0)) restore();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("document", originalDocumentDescriptor);
});

interface Mounted {
  current: () => AppState;
  doc: DocStub;
}

async function mountHook(initial: AppState): Promise<Mounted> {
  let latest = initial;
  let doc!: DocStub;
  function Probe(): React.ReactNode {
    const [state, setState] = React.useState(initial);
    latest = state;
    useUnreadSessionTracking({
      unreadSessionKeys: state.unreadSessionKeys,
      currentSessionCacheKey: SESSION_CACHE_KEY,
      setState,
    });
    return null;
  }
  doc = installDoc(true, true);
  await act(async () => { renderer = create(<Probe />); });
  return { current: () => latest, doc };
}

describe("useUnreadSessionTracking", () => {
  test("当前会话可见且有焦点时把它标记为已读", async () => {
    const mounted = await mountHook(
      appState({ unreadSessionKeys: new Set([SESSION_CACHE_KEY, "other::key"]) }),
    );

    expect(mounted.current().unreadSessionKeys.has(SESSION_CACHE_KEY)).toBe(false);
    // 其他会话的未读标记必须保留，只有当前会话被清除。
    expect(mounted.current().unreadSessionKeys.has("other::key")).toBe(true);
  });

  test("重新可见并聚焦时通过 visibilitychange 再次标记已读", async () => {
    const mounted = await mountHook(appState());
    act(() => {
      mounted.doc.visibilityState = "hidden";
    });
    act(() => {
      mounted.doc.visibilityState = "visible";
      mounted.doc.fire("visibilitychange");
    });
    expect(mounted.current().unreadSessionKeys.has(SESSION_CACHE_KEY)).toBe(false);
  });

  test("页面不可见时不标记已读，保留未读集合", async () => {
    let latest = appState({ unreadSessionKeys: new Set([SESSION_CACHE_KEY]) });
    function Probe(): React.ReactNode {
      const [state, setState] = React.useState(latest);
      latest = state;
      useUnreadSessionTracking({
        unreadSessionKeys: state.unreadSessionKeys,
        currentSessionCacheKey: SESSION_CACHE_KEY,
        setState,
      });
      return null;
    }
    installDoc(false, true);
    await act(async () => { renderer = create(<Probe />); });

    expect(latest.unreadSessionKeys.has(SESSION_CACHE_KEY)).toBe(true);
  });

  test("未读集合变化时写回存储边界", async () => {
    const writer = spyOn(storage, "writeUnreadSessionKeys").mockImplementation(
      () => undefined,
    );
    restores.push(() => writer.mockRestore());

    await mountHook(appState({ unreadSessionKeys: new Set([SESSION_CACHE_KEY]) }));

    expect(writer).toHaveBeenCalled();
  });
});
