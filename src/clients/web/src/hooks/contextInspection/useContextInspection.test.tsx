import React from "react";
import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as client from "../../api/session/sessionContext";
import type { SessionContextReadResultDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import type { ContextItem } from "../../state/contextInspection/pagination";
import { useContextInspection, type InspectionOwner } from "./useContextInspection";

interface Pending {
  session: InspectionOwner["session"];
  view: "assembly" | "assemblies";
  signal: AbortSignal;
  assemblyRef?: string | null;
  resolve: (page: SessionContextReadResultDTO) => void;
  reject: (error: Error) => void;
}

let root: ReactTestRenderer | undefined;
let restore = () => {};
afterEach(() => { act(() => root?.unmount()); root = undefined; restore(); });

function controlledReader() {
  const pending: Pending[] = [];
  const reader = spyOn(client, "readSessionContext").mockImplementation((_port, _workspace, session, view, options) =>
    new Promise<SessionContextReadResultDTO>((resolve, reject) => pending.push({ session, view, signal: options.signal, assemblyRef: options.assemblyRef, resolve, reject })),
  );
  restore = () => reader.mockRestore();
  return pending;
}

function finish(pending: Pending, items: ContextItem[] = []) {
  const session = pending.session;
  if (!session) throw new Error("测试 Session 缺失");
  pending.resolve({ resource_identity: session.resource_identity, vrn: session.vrn,
    scope: session.scope, view: pending.view, assembly_ref: pending.assemblyRef ?? undefined,
    revision: pending.assemblyRef ?? session.vrn,
    items, partial_errors: [], has_more: false });
}

function assemblyItem(pending: Pending, assemblyId: string, assemblyRef: string): ContextItem {
  const session = pending.session;
  if (!session) throw new Error("测试 Session 缺失");
  return {
    kind: "assembly",
    resource_identity: session.resource_identity,
    vrn: session.vrn,
    scope: session.scope,
    assembly_ref: assemblyRef,
    data: { assembly_id: assemblyId },
    tool_summary: [],
    tool_calls: [],
    tool_results: [],
  };
}

async function waitForPending(pending: Pending[], count: number) {
  const deadline = Date.now() + 1000;
  while (pending.length < count && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  expect(pending).toHaveLength(count);
}

const owner: InspectionOwner = {
  port: 49123,
  gatewayWorkspaceId: "gateway-workspace",
  session: {
    session_id: "session",
    resource_identity: "session-identity",
    vrn: "boxteam://workspace/backend-workspace/resources/session/session",
    scope: "workspace",
  } as InspectionOwner["session"],
  active: true,
};

test("非诊断视图不请求；迟到的旧 assembly 响应不能覆盖新选择", async () => {
  const pending = controlledReader();
  let state!: ReturnType<typeof useContextInspection>;
  function Probe(props: InspectionOwner) { state = useContextInspection(props); return null; }
  await act(async () => { root = create(<Probe {...owner} active={false} />); });
  expect(pending).toHaveLength(0);
  await act(async () => {
    root!.update(<Probe {...owner} />);
    await waitForPending(pending, 1);
  });
  await act(async () => finish(pending[0], [
    assemblyItem(pending[0], "assembly-a", "api-ref-a"),
    assemblyItem(pending[0], "assembly-b", "api-ref-b"),
  ]));
  await act(async () => {
    void state.loadProjection(state.catalog.items[0]!, true);
    await waitForPending(pending, 2);
  });
  await act(async () => {
    void state.loadProjection(state.catalog.items[1]!, true);
    await waitForPending(pending, 3);
  });
  expect(pending[1].signal.aborted).toBe(true);
  expect(pending[2].assemblyRef).toBe("api-ref-b");
  await act(async () => { finish(pending[2]); finish(pending[1]); });
  expect(state.assemblyId).toBe("assembly-b");
  expect(state.assemblyRef).toBe("api-ref-b");
  expect(state.projection.revision).toBe(pending[2].assemblyRef ?? null);
  expect(state.projectionLoading).toBe(false);
});

test("隐藏中断请求后再打开会重新读取，不残留 loading 或吞掉 source 错误", async () => {
  const pending = controlledReader();
  let state!: ReturnType<typeof useContextInspection>;
  function Probe(props: InspectionOwner) { state = useContextInspection(props); return null; }
  await act(async () => {
    root = create(<Probe {...owner} />);
    await waitForPending(pending, 1);
  });
  await act(async () => finish(pending[0], [assemblyItem(pending[0], "assembly-a", "api-ref-a")]));
  await act(async () => {
    void state.loadProjection(state.catalog.items[0]!, true);
    await waitForPending(pending, 2);
  });
  await act(async () => root!.update(<Probe {...owner} active={false} />));
  expect(pending[1].signal.aborted).toBe(true);
  expect(state.projectionLoading).toBe(false);
  await act(async () => {
    root!.update(<Probe {...owner} />);
    await waitForPending(pending, 4);
  });
  expect(pending).toHaveLength(4);
  await act(async () => {
    finish(pending[1]);
    finish(pending[2], [assemblyItem(pending[2], "assembly-a", "api-ref-a")]);
    pending[3].reject(new Error("source-mismatch"));
  });
  expect(state.projectionError).toContain("source-mismatch");
  expect(state.projection.items).toEqual([]);
  expect(state.projectionLoading).toBe(false);
});
