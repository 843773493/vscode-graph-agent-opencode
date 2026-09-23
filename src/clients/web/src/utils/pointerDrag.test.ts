import { afterEach, describe, expect, test } from "bun:test";
import { installPointerDrag } from "./pointerDrag";
import { restoreGlobalDescriptor } from "../tests/testGlobals";

type PointerListener = (event: PointerEvent) => void;

interface FakePointerHost {
  /** 按发生顺序记录监听注册/摘除、body class 增删与事件派发。 */
  log: string[];
  /** 当前挂在 body 上的 class。 */
  classes: Set<string>;
  listenerCount: (type: string) => number;
  dispatch: (type: "pointermove" | "pointerup" | "pointercancel", clientY: number) => void;
}

const CLASS_NAME = "is-drag-teardown-test";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

/** 用假 window/document 替换全局，避免依赖 jsdom，同时精确记录收尾调用顺序。 */
function installFakePointerHost(): FakePointerHost {
  const log: string[] = [];
  const classes = new Set<string>();
  const listeners = new Map<string, Set<PointerListener>>();

  const windowTarget = {
    addEventListener(type: string, listener: PointerListener) {
      log.push(`add:${type}`);
      let registered = listeners.get(type);
      if (!registered) {
        registered = new Set<PointerListener>();
        listeners.set(type, registered);
      }
      registered.add(listener);
    },
    removeEventListener(type: string, listener: PointerListener) {
      log.push(`remove:${type}`);
      listeners.get(type)?.delete(listener);
    },
  };

  const bodyClassList = {
    add(name: string) {
      classes.add(name);
      log.push(`class-add:${name}`);
    },
    remove(name: string) {
      classes.delete(name);
      log.push(`class-remove:${name}`);
    },
  };

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: windowTarget,
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: { body: { classList: bodyClassList } },
  });

  return {
    log,
    classes,
    listenerCount: (type) => listeners.get(type)?.size ?? 0,
    dispatch: (type, clientY) => {
      log.push(`dispatch:${type}`);
      const event = { type, clientY } as unknown as PointerEvent;
      // 快照派发，模拟浏览器在监听被摘除后不再回访已移除的监听。
      for (const listener of [...(listeners.get(type) ?? [])]) {
        listener(event);
      }
    },
  };
}

afterEach(() => {
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("document", originalDocumentDescriptor);
});

describe("installPointerDrag", () => {
  test("进入拖拽态时先挂 body class，再注册 pointermove/pointerup/pointercancel", () => {
    const host = installFakePointerHost();

    installPointerDrag(CLASS_NAME, () => undefined, () => undefined);

    expect(host.classes.has(CLASS_NAME)).toBe(true);
    expect(host.listenerCount("pointermove")).toBe(1);
    expect(host.listenerCount("pointerup")).toBe(1);
    expect(host.listenerCount("pointercancel")).toBe(1);
    expect(host.log).toEqual([
      `class-add:${CLASS_NAME}`,
      "add:pointermove",
      "add:pointerup",
      "add:pointercancel",
    ]);
  });

  test("pointerup 收尾严格按“摘监听 → 移除 body class → onFinish”顺序执行", () => {
    const host = installFakePointerHost();
    let listenersAtFinish = -1;
    // 初值为 true：若 onFinish 根本没被调用，断言会失败而不是被 null 掩盖。
    let classAtFinish = true;

    installPointerDrag(
      CLASS_NAME,
      () => host.log.push("move"),
      () => {
        // 在 onFinish 内部取快照，直接证明收尾动作已经发生在回调之前。
        listenersAtFinish = host.listenerCount("pointermove")
          + host.listenerCount("pointerup")
          + host.listenerCount("pointercancel");
        classAtFinish = host.classes.has(CLASS_NAME);
        host.log.push("finish");
      },
    );

    host.dispatch("pointerup", 320);

    expect(host.log).toEqual([
      `class-add:${CLASS_NAME}`,
      "add:pointermove",
      "add:pointerup",
      "add:pointercancel",
      "dispatch:pointerup",
      "remove:pointermove",
      "remove:pointerup",
      "remove:pointercancel",
      `class-remove:${CLASS_NAME}`,
      "finish",
    ]);
    expect(listenersAtFinish).toBe(0);
    expect(classAtFinish).toBe(false);
    expect(host.classes.has(CLASS_NAME)).toBe(false);
  });

  test("pointercancel 复用 pointerup 的收尾路径，收尾后不再响应指针事件", () => {
    const host = installFakePointerHost();
    let finishCalls = 0;

    installPointerDrag(
      CLASS_NAME,
      () => host.log.push("move"),
      () => {
        finishCalls += 1;
        host.log.push("finish");
      },
    );

    host.dispatch("pointercancel", 320);

    expect(host.log).toEqual([
      `class-add:${CLASS_NAME}`,
      "add:pointermove",
      "add:pointerup",
      "add:pointercancel",
      "dispatch:pointercancel",
      "remove:pointermove",
      "remove:pointerup",
      "remove:pointercancel",
      `class-remove:${CLASS_NAME}`,
      "finish",
    ]);
    expect(finishCalls).toBe(1);

    // 收尾已完成，后续 pointerup 不应再次触发 onFinish。
    host.dispatch("pointerup", 320);
    expect(finishCalls).toBe(1);
    expect(host.listenerCount("pointermove")).toBe(0);
    expect(host.listenerCount("pointerup")).toBe(0);
    expect(host.listenerCount("pointercancel")).toBe(0);
  });

  test("返回的清理函数可提前结束拖拽；重复调用不抛错但会再次触发 onFinish", () => {
    const host = installFakePointerHost();
    let finishCalls = 0;

    const cleanup = installPointerDrag(
      CLASS_NAME,
      () => host.log.push("move"),
      () => {
        finishCalls += 1;
        host.log.push("finish");
      },
    );

    host.dispatch("pointermove", 300);
    expect(host.log).toContain("move");

    cleanup();
    expect(finishCalls).toBe(1);
    expect(host.listenerCount("pointermove")).toBe(0);
    expect(host.listenerCount("pointerup")).toBe(0);
    expect(host.listenerCount("pointercancel")).toBe(0);
    expect(host.classes.has(CLASS_NAME)).toBe(false);

    // 实际行为：清理函数不是幂等的，onFinish 会被再次调用；
    // useBottomPanelResize / useMainAreaResize 依赖把 ref 置空来保证只结束一次。
    expect(() => cleanup()).not.toThrow();
    expect(finishCalls).toBe(2);
    expect(host.classes.has(CLASS_NAME)).toBe(false);
  });
});
