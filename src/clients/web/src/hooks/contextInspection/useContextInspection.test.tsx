import React from "react";
import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as client from "../../api/sessionContext";
import type { SessionContextReadResultDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { useContextInspection, type InspectionOwner } from "./useContextInspection";

interface Pending {
  resource: string;
  view: string;
  signal: AbortSignal;
  resolve: (page: SessionContextReadResultDTO) => void;
  reject: (error: Error) => void;
}

let root: ReactTestRenderer | undefined;
let restore = () => {};
afterEach(() => { act(() => root?.unmount()); root = undefined; restore(); });

function controlledReader() {
  const pending: Pending[] = [];
  const reader = spyOn(client, "readSessionContext").mockImplementation((_port, _workspace, resource, view, options) =>
    new Promise<SessionContextReadResultDTO>((resolve, reject) => pending.push({ resource, view, signal: options.signal, resolve, reject })),
  );
  restore = () => reader.mockRestore();
  return pending;
}

function finish(pending: Pending) {
  pending.resolve({ resource: pending.resource, view: pending.view, revision: pending.resource,
    items: [], partial_errors: [], has_more: false });
}

const owner: InspectionOwner = { port: 49123, workspaceId: "workspace", sessionId: "session", active: true };

test("非诊断视图不请求；迟到的旧 assembly 响应不能覆盖新选择", async () => {
  const pending = controlledReader();
  let state!: ReturnType<typeof useContextInspection>;
  function Probe(props: InspectionOwner) { state = useContextInspection(props); return null; }
  await act(async () => { root = create(<Probe {...owner} active={false} />); });
  expect(pending).toHaveLength(0);
  await act(async () => root!.update(<Probe {...owner} />));
  await act(async () => finish(pending[0]));
  await act(async () => { void state.loadProjection("assembly-a", true); });
  await act(async () => { void state.loadProjection("assembly-b", true); });
  expect(pending[1].signal.aborted).toBe(true);
  await act(async () => { finish(pending[2]); finish(pending[1]); });
  expect(state.assemblyId).toBe("assembly-b");
  expect(state.projection.revision).toEndWith("#assembly=assembly-b");
  expect(state.projectionLoading).toBe(false);
});

test("隐藏中断请求后再打开会重新读取，不残留 loading 或吞掉 source 错误", async () => {
  const pending = controlledReader();
  let state!: ReturnType<typeof useContextInspection>;
  function Probe(props: InspectionOwner) { state = useContextInspection(props); return null; }
  await act(async () => { root = create(<Probe {...owner} />); });
  await act(async () => finish(pending[0]));
  await act(async () => { void state.loadProjection("assembly-a", true); });
  await act(async () => root!.update(<Probe {...owner} active={false} />));
  expect(pending[1].signal.aborted).toBe(true);
  expect(state.projectionLoading).toBe(false);
  await act(async () => root!.update(<Probe {...owner} />));
  expect(pending).toHaveLength(4);
  await act(async () => { finish(pending[2]); pending[3].reject(new Error("source-mismatch")); });
  expect(state.projectionError).toContain("source-mismatch");
  expect(state.projection.items).toEqual([]);
  expect(state.projectionLoading).toBe(false);
});
