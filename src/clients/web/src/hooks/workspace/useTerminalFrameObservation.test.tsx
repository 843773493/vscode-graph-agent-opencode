import { afterEach, expect, mock, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/workspaceResources";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import { useTerminalFrameObservation } from "./useTerminalFrameObservation";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
let renderer: ReactTestRenderer | undefined;
let restore: (() => void) | undefined;

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  restore?.();
  restoreGlobalDescriptor("window", originalWindow);
});

test("宿主仅接收所属 iframe 的订阅，按资源和页面代际转发，卸载释放", async () => {
  const host = Object.assign(new EventTarget(), { location: { origin: "http://localhost" } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: host });
  const postMessage = mock(() => {});
  const child = { postMessage } as unknown as Window;
  const frame = { current: { contentWindow: child } as HTMLIFrameElement };
  const observations: Array<{ changed: (ids: string[] | null) => void; signal: AbortSignal }> = [];
  const observe = spyOn(api, "observeTerminalResources").mockImplementation((_port, _scope, changed, signal) => {
    observations.push({ changed, signal });
    return new Promise<void>(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
  });
  restore = () => observe.mockRestore();
  const source = "http://localhost/terminal/?terminalId=term_a&workspaceId=workspace&target_gateway_id=gateway";
  function Probe() { useTerminalFrameObservation(frame, source); return null; }
  await act(async () => { renderer = create(<Probe />); });
  function send(type: string, subscriptionId: string, origin = host.location.origin, sender: Window = child) {
    host.dispatchEvent(Object.assign(new Event("message"), {
      origin, source: sender, data: { type: `boxteam.terminal.${type}`, terminalId: "term_a", subscriptionId },
    }));
  }
  send("observe", "old", "https://another.test");
  send("observe", "old", host.location.origin, {} as Window);
  expect(observations).toHaveLength(0);
  send("observe", "old");
  send("observe", "old");
  expect(observations).toHaveLength(1);
  observations[0].changed(["term_b"]);
  expect(postMessage).not.toHaveBeenCalled();
  observations[0].changed(["term_a"]);
  expect(postMessage).toHaveBeenLastCalledWith({
    type: "boxteam.terminal.resources", terminalId: "term_a", subscriptionId: "old", error: undefined,
  }, host.location.origin);
  send("observe", "new");
  expect(observations[0].signal.aborted).toBe(true);
  send("unobserve", "old");
  expect(observations[1].signal.aborted).toBe(false);
  observations[1].changed(null);
  expect(postMessage).toHaveBeenCalledTimes(2);
  send("unobserve", "new");
  expect(observations[1].signal.aborted).toBe(true);
  send("observe", "final");
  act(() => renderer!.unmount()); renderer = undefined;
  expect(observations[2].signal.aborted).toBe(true);
  send("observe", "late");
  expect(observations).toHaveLength(3);
});
