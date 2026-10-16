import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api/workspaceResources";
import type { WorkspaceResourceDTO } from "../../api/workspaceResources";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import { useWorkspaceTerminalResources } from "./useWorkspaceTerminalResources";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const renderers: ReactTestRenderer[] = [];
const restores: Array<() => void> = [];

afterEach(() => {
  for (const renderer of renderers.splice(0)) act(() => renderer.unmount());
  for (const restore of restores.splice(0)) restore();
  restoreGlobalDescriptor("window", originalWindow);
});

function resource(revision = 1, id = "term_current"): WorkspaceResourceDTO {
  return {
    resource_id: id, workspace_id: "backend-uuid", kind: "terminal", retention_scope: "thread",
    source_thread: { session_id: "ses_owner", thread_id: "thr_actual" },
    thread_associations: [{ session_id: "ses_owner", thread_id: "thr_actual" }],
    owner_revision: revision, status: "running", reconciliation_status: "verified", available_actions: ["promote", "detach"],
    metadata: { title: `版本 ${revision}` }, created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-05T00:00:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function mount() {
  let current!: ReturnType<typeof useWorkspaceTerminalResources>;
  let poll = () => {};
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    setInterval: (callback: () => void) => { poll = callback; return 1; }, clearInterval: () => {},
  } });
  function Probe({ workspaceId }: { workspaceId: string }) {
    current = useWorkspaceTerminalResources(49_945, workspaceId, true);
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<Probe workspaceId="gateway-a" />); });
  renderers.push(renderer);
  return {
    current: () => current,
    poll: () => poll(),
    switchTo: async (workspaceId: string) => { await act(async () => renderer.update(<Probe workspaceId={workspaceId} />)); },
  };
}

function stubList() {
  const list = spyOn(api, "listWorkspaceTerminals").mockResolvedValue({ workspace_id: "backend-uuid", items: [resource()] });
  restores.push(() => list.mockRestore());
  return list;
}

describe("工作区 Terminal owner 镜像", () => {
  test("管理成功的完整对象不会被开始前的迟到刷新覆盖", async () => {
    const oldList = deferred<Awaited<ReturnType<typeof api.listWorkspaceTerminals>>>();
    const list = stubList().mockImplementation(() => oldList.promise);
    const promoted = { ...resource(2), retention_scope: "workspace" };
    const control = spyOn(api, "controlWorkspaceResource").mockResolvedValue(promoted);
    restores.push(() => control.mockRestore());
    const mounted = await mount();
    await act(async () => { await mounted.current().control(resource(), "promote"); });
    await act(async () => { oldList.resolve({ workspace_id: "backend-uuid", items: [resource()] }); });
    expect(list).toHaveBeenCalledTimes(1);
    expect(mounted.current().items).toEqual([promoted]);
    expect(mounted.current().loading).toBe(false);
  });

  test("管理进行中暂停轮询并拒绝重叠写入", async () => {
    const list = stubList();
    const response = deferred<WorkspaceResourceDTO>();
    const control = spyOn(api, "controlWorkspaceResource").mockImplementation(() => response.promise);
    restores.push(() => control.mockRestore());
    const mounted = await mount();
    let operation!: Promise<WorkspaceResourceDTO>;
    act(() => { operation = mounted.current().control(resource(), "promote"); });
    expect(mounted.current().mutating).toBe(true);
    act(() => mounted.poll());
    expect(list).toHaveBeenCalledTimes(1);
    await expect(mounted.current().control(resource(), "stop")).rejects.toThrow("尚未完成");
    await act(async () => { response.resolve(resource(2)); await operation; });
    expect(mounted.current().mutating).toBe(false);
  });

  test("失败后重取完整 owner 对象，自动轮询保留操作错误", async () => {
    const list = stubList();
    const control = spyOn(api, "controlWorkspaceResource").mockRejectedValue(new Error("409 版本冲突"));
    restores.push(() => control.mockRestore());
    const mounted = await mount();
    const authoritative = { ...resource(4), thread_associations: [] };
    list.mockResolvedValue({ workspace_id: "backend-uuid", items: [authoritative] });
    await act(async () => { await expect(mounted.current().control(resource(), "detach")).rejects.toThrow("版本冲突"); });
    expect(mounted.current().items).toEqual([authoritative]);
    await act(async () => { await mounted.current().refresh(false); });
    expect(mounted.current().error).toContain("409 版本冲突");
    await act(async () => { await mounted.current().refresh(); });
    expect(mounted.current().error).toBeNull();
  });

  test("工作区 A→B→A 后旧创建回执不能加入新视图", async () => {
    stubList();
    const response = deferred<WorkspaceResourceDTO>();
    const createTerminal = spyOn(api, "createWorkspaceTerminal").mockImplementation(() => response.promise);
    restores.push(() => createTerminal.mockRestore());
    const mounted = await mount();
    let operation!: Promise<WorkspaceResourceDTO | string>;
    act(() => { operation = mounted.current().create({ session_id: "ses_owner", thread_id: "thr_actual" }, "workspace").catch((cause: unknown) => String(cause)); });
    expect(createTerminal).toHaveBeenCalledWith(49_945, "gateway-a", { session_id: "ses_owner", thread_id: "thr_actual" }, "workspace");
    await mounted.switchTo("gateway-b");
    await mounted.switchTo("gateway-a");
    await act(async () => { response.resolve(resource(1, "term_old_create")); await operation; });
    expect(mounted.current().items.map((item) => item.resource_id)).toEqual(["term_current"]);
    expect(mounted.current().error).toBeNull();
    expect(await operation).toContain("原工作区");
  });
});
