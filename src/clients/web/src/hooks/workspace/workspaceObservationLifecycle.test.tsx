import { describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api";
import { useWorkspaceFileWatch } from "./useWorkspaceFileWatch";
import {
  pauseWorkspaceObservations,
  registerWorkspaceObservation,
  useWorkspaceObservationState,
} from "./workspaceObservationLifecycle";

describe("Backend 动作期间本页观察流生命周期", () => {
  test("按工作区同步中止，重叠动作只在全部结束后恢复", () => {
    const a = new AbortController(), b = new AbortController(), c = new AbortController();
    const unregister = [
      registerWorkspaceObservation("observer-a", a),
      registerWorkspaceObservation("observer-b", b),
      registerWorkspaceObservation("observer-c", c),
    ];
    let snapshot = { a: false, b: false, c: false };
    function Harness() {
      const a = useWorkspaceObservationState("observer-a");
      const b = useWorkspaceObservationState("observer-b");
      const c = useWorkspaceObservationState("observer-c");
      snapshot = { a: a.paused, b: b.paused, c: c.paused };
      return null;
    }
    let renderer!: ReactTestRenderer;
    let first!: () => void, second!: () => void;
    act(() => { renderer = create(<Harness />); });
    try {
      act(() => {
        first = pauseWorkspaceObservations(["observer-a", "observer-b", "observer-a"]);
        second = pauseWorkspaceObservations(["observer-b"]);
      });
      expect(a.signal.aborted).toBe(true);
      expect(b.signal.aborted).toBe(true);
      expect(c.signal.aborted).toBe(false);
      expect(snapshot).toEqual({ a: true, b: true, c: false });
      const late = new AbortController();
      const unregisterLate = registerWorkspaceObservation("observer-b", late);
      expect(late.signal.aborted).toBe(true);
      unregisterLate();
      act(first);
      expect(snapshot).toEqual({ a: false, b: true, c: false });
      act(second);
      expect(snapshot).toEqual({ a: false, b: false, c: false });
    } finally {
      act(() => { first?.(); second?.(); renderer.unmount(); });
      for (const dispose of unregister) dispose();
    }
  });

  test("复用文件流原 owner，暂停不重连，立即结束也恢复，卸载仍中止", async () => {
    const controllers: AbortSignal[] = [];
    const stream = spyOn(api, "streamWorkspaceFileEvents").mockImplementation(async (_port, _paths, options) => {
      const signal = options!.signal!;
      controllers.push(signal);
      options?.onConnected?.();
      if (signal.aborted) return;
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });
    const noop = () => {};
    function Harness() {
      useWorkspaceFileWatch({
        active: true, port: 49_740, workspaceId: "observer-file",
        paths: ["/workspace"], onOverflow: noop, onStatusChange: noop,
      });
      return null;
    }
    let renderer!: ReactTestRenderer;
    let resume: (() => void) | undefined;
    try {
      await act(async () => { renderer = create(<Harness />); });
      expect(controllers).toHaveLength(1);
      await act(async () => { resume = pauseWorkspaceObservations(["observer-file"]); });
      expect(controllers[0].aborted).toBe(true);
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
      expect(controllers).toHaveLength(1);
      await act(async () => { resume!(); });
      expect(controllers).toHaveLength(2);
      await act(async () => {
        const finish = pauseWorkspaceObservations(["observer-file"]);
        finish();
      });
      expect(controllers[1].aborted).toBe(true);
      expect(controllers).toHaveLength(3);
      await act(async () => { renderer.unmount(); });
      expect(controllers[2].aborted).toBe(true);
    } finally {
      await act(async () => { resume?.(); renderer?.unmount(); });
      stream.mockRestore();
    }
  });
});
