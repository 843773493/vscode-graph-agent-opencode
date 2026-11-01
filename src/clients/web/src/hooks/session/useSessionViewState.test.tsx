import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { act } from "react-test-renderer";
import * as views from "./browserSessionViews";
import type { SessionViewState } from "../../state/sessionViewState";
import type { AppState } from "../../types/frontend";
import { mountViewStateController } from "../../tests/fixtures/session/sessionViewState";

const value: SessionViewState = { workspace_id: "workspace", session_id: "session", turn_anchor: "turn-1", scroll_offset: 45,
  follow_latest: false, projection_version: 1, tool_details_expanded: true, updated_at: "2026-10-30T00:00:00Z" };
const mounted: Array<{ unmount: () => void }> = [];
const restores: Array<() => void> = [];
afterEach(() => { mounted.splice(0).forEach(item => item.unmount()); restores.splice(0).forEach(restore => restore()); });
async function mount(threadId?: string) {
  const instance = await mountViewStateController({ initial: { currentSession: { session_id: "session" }, currentSessionWorkspaceId: "workspace",
    gatewayUserViewStates: new Map(), expandDetails: false, status: "" } as unknown as AppState,
    host: state => ({ apiPort: 0, currentWorkspaceId: "workspace", currentSessionId: "session", threadId,
      scopeKey: threadId,
      gatewayUserViewStates: state.gatewayUserViewStates, expandDetails: state.expandDetails }) });
  mounted.push(instance); return instance;
}

describe("浏览器会话视图", () => {
  test("未选择用户也恢复位置，并发读取只发起一次", async () => {
    let finish!: (view: SessionViewState) => void;
    const reader = spyOn(views, "readSessionView").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    restores.push(() => reader.mockRestore());
    const m = await mount();
    const first = m.controller().loadSessionViewState("workspace", "session");
    const second = m.controller().loadSessionViewState("workspace", "session");
    expect(reader).toHaveBeenCalledTimes(1);
    await act(async () => { finish(value); await first; await second; });
    expect(m.state().gatewayUserViewStates.get("workspace::session")).toEqual(value);
    expect(m.state().expandDetails).toBe(true);
  });
  test("Thread 读写精确携带 owner，保存镜像来自持久化返回值", async () => {
    const reader = spyOn(views, "readSessionView").mockResolvedValue(null);
    const writer = spyOn(views, "writeSessionView").mockResolvedValue(value);
    restores.push(() => reader.mockRestore(), () => writer.mockRestore());
    const m = await mount("thread-b");
    await act(async () => { await m.controller().loadSessionViewState("workspace", "session"); });
    expect(reader.mock.calls[0]).toEqual([0, "workspace", "session", "thread-b"]);
    await act(async () => { m.controller().saveSessionViewState({ turn_anchor: "turn-1", scroll_offset: 45, follow_latest: false }); });
    expect(writer.mock.calls[0]?.[4]).toBe("thread-b");
    expect(m.state().gatewayUserViewStates.get("thread-b")).toEqual(value);
  });
  test("旧读取完成不能覆盖用户后续保存的位置", async () => {
    let finish!: (view: SessionViewState) => void;
    const reader = spyOn(views, "readSessionView").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const writer = spyOn(views, "writeSessionView").mockResolvedValue({ ...value, turn_anchor: "turn-new" });
    restores.push(() => reader.mockRestore(), () => writer.mockRestore());
    const m = await mount();
    const pending = m.controller().loadSessionViewState("workspace", "session");
    await act(async () => { m.controller().saveSessionViewState({ turn_anchor: "turn-new", scroll_offset: 0, follow_latest: false }); });
    await act(async () => { finish(value); await pending; });
    expect(m.state().gatewayUserViewStates.get("workspace::session")?.turn_anchor).toBe("turn-new");
  });
  test("读取失败和没有保存的视图不同，故障明确显示", async () => {
    const reader = spyOn(views, "readSessionView").mockRejectedValue(new Error("数据库不可读"));
    restores.push(() => reader.mockRestore());
    const m = await mount();
    await act(async () => { expect((await m.controller().loadSessionViewState("workspace", "session")).kind).toBe("failed"); });
    expect(m.state().status).toBe("读取浏览器视图位置失败: 数据库不可读");
  });
  test("保存失败交给宿主错误出口，保留先前持久化镜像", async () => {
    const reader = spyOn(views, "readSessionView").mockResolvedValue(value);
    const writer = spyOn(views, "writeSessionView").mockRejectedValue(new Error("事务提交失败"));
    restores.push(() => reader.mockRestore(), () => writer.mockRestore());
    const m = await mount("thread-b");
    await act(async () => { await m.controller().loadSessionViewState("workspace", "session"); });
    await act(async () => { m.controller().saveSessionViewState({ turn_anchor: "unsaved", scroll_offset: 99, follow_latest: false }); });
    expect(m.state().status).toBe("保存浏览器视图位置失败: 事务提交失败");
    expect(m.state().gatewayUserViewStates.get("thread-b")).toEqual(value);
  });
});
