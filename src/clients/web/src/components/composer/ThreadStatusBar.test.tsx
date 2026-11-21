import { expect, test } from "bun:test";
import React, { useState } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import ThreadStatusBar, { type ThreadStatusBarProps } from "./ThreadStatusBar";
import Actions from "./ComposerActionButtons";
import { ComposerDiagnosticsContext } from "./ComposerDiagnosticsContext";
import { conversation } from "../chat/turn/chatTurnTestFixtures";
import { createSessionTurnTimeline } from "../../state/session/turnTimeline";
import type { ComposerTasks } from "./ComposerTaskList";

function props(): ThreadStatusBarProps {
  return { conversations: [], timeline: { ...createSessionTurnTimeline("scope"), phase: "ready" }, activeJobId: null, active: true };
}
const queued = { ...conversation("queued"), pending: true, deliveryPolicy: "after_turn" as const, queueSnapshotVersion: 7 };
const tasks: ComposerTasks = { items: [{
  task_id: "goal", workspace_id: "workspace", revision: 1, definition_revision: 1,
  tokens_used: 0, time_used_seconds: 0, sync_version: 1,
  application_type: "goal", application_version: "1", configuration: undefined,
  lifecycle: "active", phase: "waiting_trigger", recovering: false, created_at: undefined, updated_at: undefined,
}], loaded: true, error: null, onAction: async () => {}, onManage() {} };
function click(renderer: ReactTestRenderer, section: string) {
  act(() => renderer.root.findByProps({ "data-status-section": section }).props.onClick());
}
function panel(renderer: ReactTestRenderer, section: string) {
  return renderer.root.findByProps({ "data-status-panel": section });
}
function visiblePanels(renderer: ReactTestRenderer) {
  return renderer.root.findAllByProps({ role: "region" }).filter(node => !node.props.hidden).map(node => node.props["data-status-panel"]);
}

test("执行阶段、缓存恢复和同步失败均不会凭空增加业务入口", () => {
  const messages: string[] = [];
  const report = (message: string) => messages.push(message);
  let renderer!: ReactTestRenderer;
  const view = (value: ThreadStatusBarProps) => <ComposerDiagnosticsContext.Provider value={report}><ThreadStatusBar {...value} /></ComposerDiagnosticsContext.Provider>;
  act(() => { renderer = create(view(props())); });
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(view({ ...props(), conversations: [conversation("running")], activeJobId: "job_1" })));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(view({ ...props(), timeline: { ...props().timeline!, restoredFromCache: true } })));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(view({ ...props(), syncError: "HTTP 503 原始错误" })));
  expect(renderer.toJSON()).toBeNull();
  expect(messages).toEqual(["同步失败: HTTP 503 原始错误"]);
  act(() => renderer.unmount());
});

test("已有队列同步出错不改变条目、展开状态或消息内容", () => {
  let renderer!: ReactTestRenderer;
  const messages: string[] = [];
  const report = (message: string) => messages.push(message);
  const view = (syncError?: string) => <ComposerDiagnosticsContext.Provider value={report}>
    <ThreadStatusBar {...props()} conversations={[queued]} syncError={syncError} />
  </ComposerDiagnosticsContext.Provider>;
  act(() => { renderer = create(view()); });
  click(renderer, "queue");
  const before = JSON.stringify(renderer.toJSON());
  act(() => renderer.update(view("后端暂时无法同步")));
  expect(JSON.stringify(renderer.toJSON())).toBe(before);
  expect(panel(renderer, "queue").props.hidden).toBe(true);
  expect(messages[0]).toContain("后端暂时无法同步");
  act(() => renderer.unmount());
});

test("空任务不占位；非空任务默认折叠，管理按钮在折叠时仍可用", () => {
  let managed = 0;
  let renderer!: ReactTestRenderer;
  const view = (value: ComposerTasks) => <ThreadStatusBar {...props()} tasks={{ ...value, onManage: () => managed++ }} />;
  act(() => { renderer = create(view(tasks)); });
  expect(visiblePanels(renderer)).toEqual([]);
  act(() => renderer.root.findByProps({ "aria-label": "管理当前会话的持续任务" }).props.onClick());
  expect(managed).toBe(1);
  click(renderer, "tasks");
  expect(visiblePanels(renderer)).toEqual(["tasks"]);
  act(() => renderer.update(view({ ...tasks, items: [] })));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(view({ ...tasks, items: [], loaded: false })));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(view(tasks)));
  expect(visiblePanels(renderer)).toEqual([]);
  act(() => renderer.unmount());
});

test("空任务读取失败只记录原始错误，不生成任务入口", () => {
  const messages: string[] = [];
  let renderer!: ReactTestRenderer;
  const report = (message: string) => messages.push(message);
  const view = (loaded: boolean) => <ComposerDiagnosticsContext.Provider value={report}>
    <ThreadStatusBar {...props()} tasks={{ ...tasks, items: [], loaded, error: "HTTP 503" }} />
  </ComposerDiagnosticsContext.Provider>;
  act(() => { renderer = create(view(false)); });
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(view(true)));
  expect(renderer.toJSON()).toBeNull();
  expect(messages).toEqual(["持续任务: HTTP 503"]);
  act(() => renderer.unmount());
});

test("已有任务同步失败保留任务和折叠状态，并记录原始错误", () => {
  const messages: string[] = [];
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ComposerDiagnosticsContext.Provider value={message => messages.push(message)}>
    <ThreadStatusBar {...props()} tasks={{ ...tasks, error: "HTTP 503" }} />
  </ComposerDiagnosticsContext.Provider>); });
  expect(renderer.root.findByProps({ "data-status-section": "tasks" }).props.className).toContain("needs-attention");
  expect(renderer.root.findByProps({ "data-status-section": "tasks" }).props["aria-label"]).toBe("持续任务 1");
  expect(visiblePanels(renderer)).toEqual([]);
  expect(messages).toEqual(["持续任务: HTTP 503"]);
  act(() => renderer.unmount());
});

test("持续任务首次出现不展开；和队列独立折叠，内容更新不重开", () => {
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ThreadStatusBar {...props()} conversations={[queued]} />); });
  act(() => renderer.update(<ThreadStatusBar {...props()} conversations={[queued]} tasks={tasks} />));
  expect(renderer.root.findAllByType("button").filter(node => node.props["data-status-section"])
    .map(node => node.props["data-status-section"])).toEqual(["queue", "tasks"]);
  expect(visiblePanels(renderer)).toEqual(["queue"]);
  click(renderer, "tasks");
  expect(visiblePanels(renderer)).toEqual(["tasks", "queue"]);
  click(renderer, "queue");
  act(() => renderer.update(<ThreadStatusBar {...props()} conversations={[{ ...queued, waitingReason: "新的等待原因" }]} tasks={tasks} />));
  expect(panel(renderer, "queue").props.hidden).toBe(true);
  expect(panel(renderer, "tasks").props.hidden).toBe(false);
  click(renderer, "tasks");
  act(() => renderer.update(<ThreadStatusBar {...props()} conversations={[queued]} tasks={{ ...tasks, items: tasks.items.map(task => ({ ...task, revision: 2 })) }} />));
  expect(visiblePanels(renderer)).toEqual([]);
  click(renderer, "tasks");
  click(renderer, "queue");
  expect(visiblePanels(renderer)).toEqual(["queue", "tasks"]);
  act(() => renderer.unmount());
});

test("本地待确认消息与后端消息共用队列入口，折叠仍保留恢复草稿", () => {
  function Draft() {
    const [text, setText] = useState("");
    return <textarea value={text} onChange={event => setText(event.target.value)} />;
  }
  const submission = { count: 1, attention: true, content: <Draft /> };
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ThreadStatusBar {...props()} conversations={[queued]} submission={submission} />); });
  expect(renderer.root.findByProps({ "data-status-section": "queue" }).props["aria-label"]).toBe("排队 2");
  expect(renderer.root.findAllByProps({ "data-status-section": "submissions" })).toHaveLength(0);
  act(() => renderer.root.findByType("textarea").props.onChange({ target: { value: "保留的恢复输入" } }));
  click(renderer, "queue");
  act(() => renderer.update(<ThreadStatusBar {...props()} conversations={[queued]} submission={{ ...submission, attention: false }} />));
  expect(panel(renderer, "queue").props.hidden).toBe(true);
  click(renderer, "queue");
  expect(renderer.root.findByType("textarea").props.value).toBe("保留的恢复输入");
  act(() => renderer.update(<ThreadStatusBar {...props()} submission={{ count: 0, attention: true, content: null }} />));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.unmount());
});

test("条目移除后不留空详情，重新出现及切换 owner 后默认展开", () => {
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ThreadStatusBar key="a" {...props()} conversations={[queued]} />); });
  click(renderer, "queue");
  act(() => renderer.update(<ThreadStatusBar key="a" {...props()} />));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.update(<ThreadStatusBar key="a" {...props()} conversations={[queued]} />));
  expect(panel(renderer, "queue").props.hidden).toBe(false);
  click(renderer, "queue");
  act(() => renderer.update(<ThreadStatusBar key="b" {...props()} conversations={[queued]} />));
  expect(panel(renderer, "queue").props.hidden).toBe(false);
  act(() => renderer.unmount());
});

test("Escape 只折叠当前详情并归焦，子控件已处理的 Escape 不抢占", () => {
  let focused: string | undefined;
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ThreadStatusBar {...props()} conversations={[queued]} tasks={tasks} />, {
    createNodeMock: element => element.type === "button" ? { focus: () => { focused = element.props["data-status-section"]; } } : null,
  }); });
  click(renderer, "tasks");
  const event = { key: "Escape", defaultPrevented: true, preventDefault() {}, stopPropagation() {} };
  act(() => panel(renderer, "queue").props.onKeyDown(event));
  expect(panel(renderer, "queue").props.hidden).toBe(false);
  act(() => panel(renderer, "queue").props.onKeyDown({ ...event, defaultPrevented: false }));
  expect(panel(renderer, "queue").props.hidden).toBe(true);
  expect(panel(renderer, "tasks").props.hidden).toBe(false);
  expect(focused).toBe("queue");
  act(() => renderer.unmount());
});

test("队列复用原投递边界、撤回及清空 writer，隐藏不丢子菜单", async () => {
  const calls: unknown[] = [];
  const value = { ...props(), conversations: [queued], queueActions: {
    onClear: async () => { calls.push("clear"); }, onRemove: async (id: string) => { calls.push(id); },
    onChangePolicy: async (...args: unknown[]) => { calls.push(args); } } };
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ThreadStatusBar {...value} />); });
  act(() => renderer.root.findByProps({ className: "chat-pending-direction-button" }).props.onClick());
  click(renderer, "queue"); click(renderer, "queue");
  expect(renderer.root.findAllByProps({ role: "menuitemradio" })).toHaveLength(3);
  await act(async () => renderer.root.findAllByProps({ role: "menuitemradio" })[1]!.props.onClick());
  expect(calls[0]).toEqual([queued.userMessage!.message_id, "after_tool_result", 7]);
  await act(async () => renderer.root.findByProps({ "aria-label": "撤回排队消息" }).props.onClick());
  expect(calls[1]).toBe(queued.userMessage!.message_id);
  await act(async () => renderer.root.findByProps({ className: "chat-pending-composer-clear" }).props.onClick());
  expect(calls[2]).toBe("clear");
  act(() => renderer.update(<ThreadStatusBar {...value} conversations={[]} />));
  expect(renderer.toJSON()).toBeNull();
  act(() => renderer.unmount());
});

test("边界切换失败只标记原消息，保留原边界，详细原因交给输出", async () => {
  const messages: string[] = [];
  const queueActions = { onClear: async () => {}, onRemove: async () => {},
    onChangePolicy: async () => { throw new Error("HTTP 409 snapshot_version=7, current=8"); } };
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<ComposerDiagnosticsContext.Provider value={message => messages.push(message)}>
    <ThreadStatusBar {...props()} conversations={[queued]} queueActions={queueActions} />
  </ComposerDiagnosticsContext.Provider>); });
  act(() => renderer.root.findByProps({ className: "chat-pending-direction-button" }).props.onClick());
  await act(async () => renderer.root.findAllByProps({ role: "menuitemradio" })[1]!.props.onClick());
  expect(renderer.root.findByProps({ className: "chat-pending-direction-button" }).props["aria-label"]).toBe("本轮结束后投递");
  expect(renderer.root.findAllByProps({ role: "listitem" })).toHaveLength(1);
  expect(renderer.root.findByProps({ role: "alert" }).type).toBe("span");
  expect(JSON.stringify(renderer.toJSON())).not.toContain("HTTP 409");
  expect(messages).toEqual([`message_id=${queued.userMessage!.message_id}: HTTP 409 snapshot_version=7, current=8`]);
  act(() => renderer.unmount());
});

test("停止请求只改变原停止按钮的忙碌状态，不增加菜单或提示块", () => {
  const callbacks = { onClear() {}, onControl() {}, onRefreshControl() {}, onSend() {}, onAlternate() {}, onToggleDefault() {} };
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<Actions {...callbacks} hasContent={false} showInterrupt controlReady stopPending defaultDeliveryPolicy="after_turn" />); });
  const stop = renderer.root.findByProps({ id: "interruptButton" });
  expect(stop.props.disabled).toBe(true);
  expect(stop.props["aria-busy"]).toBe(true);
  expect(stop.props.title).toBe("正在请求停止");
  expect(renderer.root.findAllByProps({ "data-status-section": "execution" })).toHaveLength(0);
  act(() => renderer.update(<Actions {...callbacks} hasContent={false} showInterrupt controlReady defaultDeliveryPolicy="after_turn" />));
  expect(renderer.root.findByProps({ id: "interruptButton" }).props.disabled).toBe(false);
  act(() => renderer.unmount());
});

test("空输入仅在后端允许时继续；右键立即动作和默认选择彼此独立", () => {
  const calls: string[] = [];
  const callbacks = { onClear() {}, onControl(action: string) { calls.push(action); }, onRefreshControl() {}, onSend() { calls.push("send"); }, onAlternate() {}, onToggleDefault() {} };
  const props = { ...callbacks, hasContent: false, showInterrupt: true, controlReady: true, defaultDeliveryPolicy: "after_turn" as const };
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<Actions {...props} />); });
  const send = () => renderer.root.findByProps({ id: "sendButton" });
  expect(send().props["aria-disabled"]).toBe(true);
  act(() => send().props.onClick());
  expect(calls).toEqual([]);
  const context = renderer.root.findAllByProps({ className: "composer-more-control" }).find(node => node.props.onContextMenu)!;
  act(() => context.props.onContextMenu({ preventDefault() {}, clientX: 4, clientY: 8 }));
  act(() => renderer.update(<Actions {...props} pausing advancementPaused />));
  const immediate = renderer.root.findAllByProps({ role: "menuitem" });
  expect(immediate.find(node => node.children.includes("打断当前执行"))?.props.disabled).toBe(false);
  expect(immediate.find(node => node.children.includes("完全打断整个会话"))?.props.disabled).toBe(false);
  const defaults = renderer.root.findAllByProps({ role: "menuitemradio" });
  act(() => defaults[1]!.props.onClick());
  expect(calls).toEqual([]);
  act(() => renderer.root.findByProps({ id: "interruptButton" }).props.onClick());
  expect(calls).toEqual(["interrupt"]);
  act(() => renderer.update(<Actions {...props} canResume />));
  expect(send().props["aria-label"]).toBe("继续执行");
  act(() => send().props.onClick());
  act(() => renderer.update(<Actions {...props} hasContent canResume />));
  act(() => send().props.onClick());
  expect(calls).toEqual(["interrupt", "resume", "send"]);
  act(() => renderer.unmount());
});
