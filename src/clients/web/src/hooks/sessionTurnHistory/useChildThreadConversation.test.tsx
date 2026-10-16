import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installSessionCatalogFetchMock, unwrapSessionCatalogFetch } from "../../api/session/sessionApiFetchMock";
import type { ChildThreadSummary } from "../../types/backend";
import { resolveTerminalResourceTarget, useChildThreadConversation } from "./useChildThreadConversation";

const SESSION = "ses_019fca64740070008000000000000001";
const CHILDREN: ChildThreadSummary[] = [1, 2].map((index) => ({
  thread_id: `thr_019fca6474007000800000000000000${index}`,
  created_at: "2026-10-08T00:00:00Z", status: "completed",
  report_execution_id: `execution-${index}`, report_status: "completed",
}));
let current: ReturnType<typeof useChildThreadConversation>;
let renderer: ReactTestRenderer | null = null;
const response = (data: unknown) => Response.json({ code: 0, message: "ok", request_id: "req-child", data });
const history = (threadId: string) => ({
  thread_id: threadId, source_view_id: `view-${threadId}`, source_view_revision: 1,
  items: [], internal_executions: [], projection_epoch: 1, has_before: false,
});

function Harness({
  workspaceId = "workspace-a",
  sessionId = SESSION,
  threads = CHILDREN,
  enabled = true,
}: {
  workspaceId?: string;
  sessionId?: string;
  threads?: ChildThreadSummary[];
  enabled?: boolean;
}) {
  current = useChildThreadConversation({ apiPort: 49199, workspaceId, sessionId, threads, enabled });
  return null;
}

async function until(condition: () => boolean) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (condition()) return;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1)); });
  }
  throw new Error("子线程状态未按预期收敛");
}

afterEach(() => {
  if (renderer) act(() => renderer?.unmount());
  renderer = null;
  unwrapSessionCatalogFetch();
});

test("终端目标遵循可见 child pair，未解析时停用，隐藏或关闭后回到主线程", () => {
  const main = { session_id: SESSION, thread_id: "thr_main" };
  const child = CHILDREN[0]!;
  const intent = {
    workspaceId: "workspace-a",
    sessionId: SESSION,
    threadId: child.thread_id,
    view: "history" as const,
  };
  const target = {
    childPanelVisible: true,
    selectionIntent: intent,
    selectedThread: child,
    bottomPanelWorkspaceId: "workspace-a",
    mainThread: main,
  };

  expect(resolveTerminalResourceTarget(target)).toEqual({
    thread: { session_id: SESSION, thread_id: child.thread_id },
    unavailable: false,
  });
  expect(resolveTerminalResourceTarget({ ...target, selectedThread: null })).toEqual({
    thread: null,
    unavailable: true,
  });
  expect(resolveTerminalResourceTarget({ ...target, bottomPanelWorkspaceId: "workspace-b" })).toEqual({
    thread: null,
    unavailable: true,
  });
  expect(resolveTerminalResourceTarget({ ...target, childPanelVisible: false })).toEqual({
    thread: main,
    unavailable: false,
  });
  expect(resolveTerminalResourceTarget({ ...target, selectionIntent: null })).toEqual({
    thread: main,
    unavailable: false,
  });
});

test("fork 初选在新 owner 未解析时保留 owner，加载后可选择并关闭", () => {
  const forkOwner = { workspaceId: "workspace-fork", sessionId: "ses_fork" };
  const initialThread = CHILDREN[0]!.thread_id;
  act(() => { renderer = create(<Harness enabled={false} />); });
  act(() => current.selectThreadForOwner(forkOwner, initialThread));
  expect(current.selectionIntent).toBeNull();

  act(() => renderer!.update(<Harness {...forkOwner} threads={[]} enabled={false} />));
  expect(current.selectionIntent).toEqual({ ...forkOwner, threadId: initialThread, view: "history" });
  expect(current.selectedThread).toBeNull();
  expect(resolveTerminalResourceTarget({
    childPanelVisible: true,
    selectionIntent: current.selectionIntent,
    selectedThread: current.selectedThread,
    bottomPanelWorkspaceId: forkOwner.workspaceId,
    mainThread: { session_id: forkOwner.sessionId, thread_id: "thr_fork_main" },
  })).toEqual({ thread: null, unavailable: true });

  act(() => renderer!.update(<Harness {...forkOwner} threads={CHILDREN} enabled={false} />));
  expect(current.selectedThread?.thread_id).toBe(initialThread);
  expect(resolveTerminalResourceTarget({
    childPanelVisible: true,
    selectionIntent: current.selectionIntent,
    selectedThread: current.selectedThread,
    bottomPanelWorkspaceId: forkOwner.workspaceId,
    mainThread: { session_id: forkOwner.sessionId, thread_id: "thr_fork_main" },
  })).toEqual({ thread: { session_id: forkOwner.sessionId, thread_id: initialThread }, unavailable: false });
  act(() => current.close());
  expect(current.selectionIntent).toBeNull();
});

test("冷历史和报告请求冻结 child owner，草稿和未读按 child 独立保存", async () => {
  const requests: { path: string; body: Record<string, unknown>; workspace: string | null }[] = [];
  installSessionCatalogFetchMock(({ path, init }) => {
    const body = JSON.parse(String(init?.body));
    const threadId = path.split("/").slice(-2)[0]!;
    requests.push({ path, body, workspace: new Headers(init?.headers).get("X-BoxTeam-Workspace-Id") });
    return response(body.execution_id ? {
      ...history(threadId), internal_executions: [{
        execution_id: body.execution_id, job_id: "job-internal", session_id: SESSION, thread_id: threadId,
        status: "completed", created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:01Z",
        display_parts: [], response_parts: [],
      }],
    } : history(threadId));
  });
  act(() => { renderer = create(<Harness />); });
  act(() => current.selectThread(CHILDREN[0]!.thread_id));
  await until(() => current.snapshot.history !== null);
  act(() => current.setDraft("child A 的草稿"));
  act(() => current.selectThread(CHILDREN[1]!.thread_id));
  await until(() => current.snapshot.history !== null);
  expect(current.snapshot.draft).toBe("");
  act(() => current.setDraft("child B 的草稿"));
  act(() => current.selectThread(CHILDREN[0]!.thread_id));
  await until(() => !current.snapshot.loading);
  expect(current.snapshot.draft).toBe("child A 的草稿");
  expect(current.isUnread(CHILDREN[0]!)).toBe(true);
  await act(async () => { await current.openReport(); });
  expect(current.isUnread(CHILDREN[0]!)).toBe(false);
  expect(current.isUnread(CHILDREN[1]!)).toBe(true);
  expect(requests.every((request) => request.workspace === "workspace-a" && request.path.includes("/threads/thr_"))).toBe(true);
  expect(requests[requests.length - 1]!.body).toEqual({ direction: "tail", execution_id: "execution-1", include: ["user", "text", "reasoning_detail", "encrypted_reasoning_meta", "tool_call", "tool_result", "final_response", "internal"] });
});

test("报告请求在途时重复点击同一 child 报告保留唯一读取并正常收敛", async () => {
  let complete!: (reply: Response) => void;
  let signal: AbortSignal | null = null;
  let requests = 0;
  installSessionCatalogFetchMock(({ init }) => {
    requests += 1;
    signal = init?.signal ?? null;
    return new Promise<Response>((resolve) => { complete = resolve; });
  });
  act(() => { renderer = create(<Harness />); });
  act(() => current.selectThread(CHILDREN[0]!.thread_id, "report"));
  await until(() => requests === 1 && current.snapshot.loading);
  act(() => current.selectThread(CHILDREN[0]!.thread_id, "report"));
  expect(requests).toBe(1);
  expect((signal as AbortSignal | null)?.aborted).toBe(false);
  await act(async () => {
    complete(response({ ...history(CHILDREN[0]!.thread_id), internal_executions: [{
      execution_id: "execution-1", job_id: "job-report", session_id: SESSION, thread_id: CHILDREN[0]!.thread_id,
      status: "completed", created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:01Z",
      display_parts: [], response_parts: [],
    }] }));
  });
  await until(() => !current.snapshot.loading);
  expect(current.snapshot.report?.thread_id).toBe(CHILDREN[0]!.thread_id);
  expect(current.snapshot.error).toBeNull();
});

test("报告续页合并同一 execution 的 256 加 44 个 response part 并按成员坐标排序", async () => {
  const threadId = CHILDREN[0]!.thread_id;
  const part = (
    logicalItemOrdinal: number,
    partOrdinal = 0,
    projection: "summary" | "detail" = "detail",
    text = `正文 ${logicalItemOrdinal}.${partOrdinal}`,
  ) => ({
    part_id: `item-${logicalItemOrdinal}:part:${partOrdinal}`,
    kind: "text",
    projection,
    status: "completed",
    source: {
      message_sequence: logicalItemOrdinal + 1,
      item_id: `item-${logicalItemOrdinal}`,
      item_sequence: logicalItemOrdinal + 1,
      logical_item_ordinal: logicalItemOrdinal,
      part_ordinal: partOrdinal,
      created_at: "2026-10-08T00:00:00Z",
    },
    text,
  });
  const tailParts = Array.from({ length: 256 }, (_, index) => index + 44)
    .flatMap((ordinal) => ordinal === 50
      ? [part(ordinal, 1), part(ordinal, 0)]
      : [part(ordinal, 0, ordinal === 100 ? "summary" : "detail", ordinal === 100 ? "摘要投影" : undefined)]);
  const olderParts = Array.from({ length: 44 }, (_, ordinal) => part(ordinal));
  olderParts.push(part(100, 0, "detail", "完整投影"));
  const requests: { path: string; body: Record<string, unknown> }[] = [];
  installSessionCatalogFetchMock(({ path, init }) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    requests.push({ path, body });
    const thread = path.split("/").slice(-2)[0]!;
    const isTail = body.direction === "tail";
    return response({
      ...history(thread),
      has_before: isTail,
      before_cursor: isTail ? "cursor-before-256" : null,
      internal_executions: [{
        execution_id: body.execution_id,
        job_id: "job-report",
        session_id: SESSION,
        thread_id: thread,
        status: "completed",
        created_at: "2026-10-08T00:00:00Z",
        updated_at: "2026-10-08T00:00:01Z",
        display_parts: [],
        response_parts: isTail ? tailParts : olderParts,
      }],
    });
  });

  act(() => { renderer = create(<Harness />); });
  act(() => current.selectThread(threadId, "report"));
  await until(() => !current.snapshot.loading && current.snapshot.report?.before_cursor === "cursor-before-256");
  expect(current.snapshot.report?.internal_executions?.[0]?.response_parts).toHaveLength(257);
  expect(new Set(current.snapshot.report?.internal_executions?.[0]?.response_parts.map(({ source }) => source.item_id)).size).toBe(256);

  act(() => { void current.loadEarlier(); });
  await until(() => !current.snapshot.loading && current.snapshot.report?.before_cursor === null);

  const loaded = current.snapshot.report?.internal_executions?.[0];
  expect(requests.map(({ path, body }) => ({ path, body }))).toEqual([
    {
      path: `/api/v1/sessions/${SESSION}/threads/${threadId}/history`,
      body: {
        direction: "tail",
        execution_id: "execution-1",
        include: ["user", "text", "reasoning_detail", "encrypted_reasoning_meta", "tool_call", "tool_result", "final_response", "internal"],
      },
    },
    {
      path: `/api/v1/sessions/${SESSION}/threads/${threadId}/history`,
      body: {
        direction: "before",
        cursor: "cursor-before-256",
        execution_id: "execution-1",
        include: ["user", "text", "reasoning_detail", "encrypted_reasoning_meta", "tool_call", "tool_result", "final_response", "internal"],
      },
    },
  ]);
  expect(loaded?.response_parts).toHaveLength(301);
  expect(new Set(loaded?.response_parts.map(({ part_id }) => part_id)).size).toBe(301);
  expect(loaded?.response_parts.map((entry) => [
    entry.source.logical_item_ordinal,
    entry.source.part_ordinal,
  ])).toEqual(Array.from({ length: 300 }, (_, ordinal) => ordinal === 50
    ? [[ordinal, 0], [ordinal, 1]]
    : [[ordinal, 0]]).flat());
  expect(loaded?.response_parts.filter(({ source }) => source.item_id === "item-50").map(({ part_id }) => part_id))
    .toEqual(["item-50:part:0", "item-50:part:1"]);
  expect(loaded?.response_parts.find(({ part_id }) => part_id === "item-100:part:0"))
    .toMatchObject({ projection: "detail", text: "完整投影" });
});

test("发送期间切到 sibling 后，原 child 的迟到回执不覆盖 sibling 历史", async () => {
  let completeSend: ((reply: Response) => void) | null = null;
  const sends: { path: string; workspace: string | null; content: string }[] = [];
  installSessionCatalogFetchMock(({ path, init }) => {
    if (path.endsWith("/messages")) {
      const body = JSON.parse(String(init?.body));
      sends.push({ path, workspace: new Headers(init?.headers).get("X-BoxTeam-Workspace-Id"), content: body.message.content });
      return new Promise<Response>((resolve) => { completeSend = resolve; });
    }
    return response(history(path.split("/").slice(-2)[0]!));
  });
  act(() => { renderer = create(<Harness />); });
  act(() => current.selectThread(CHILDREN[0]!.thread_id));
  await until(() => current.snapshot.history !== null);
  act(() => current.setDraft("只发给 child A"));
  let pending: Promise<void>;
  act(() => { pending = current.send(); });
  await until(() => completeSend !== null);
  act(() => current.selectThread(CHILDREN[1]!.thread_id));
  await until(() => current.snapshot.history !== null && !current.snapshot.loading);
  await act(async () => {
    completeSend!(response({ message_id: "message-a", job_id: "job-a", turn_id: "turn-a", thread_id: CHILDREN[0]!.thread_id, status: "queued", dispatch: {} }));
    await pending!;
  });
  expect(sends).toEqual([{ path: `/api/v1/sessions/${SESSION}/threads/${CHILDREN[0]!.thread_id}/messages`, workspace: "workspace-a", content: "只发给 child A" }]);
  expect(current.selectedThread?.thread_id).toBe(CHILDREN[1]!.thread_id);
  expect(current.snapshot.history?.thread_id).toBe(CHILDREN[1]!.thread_id);
  expect(current.snapshot.accepted).toBeNull();
  expect(current.snapshot.loading).toBe(false);
});

test("消息已接受后，历史读取未返回仍允许向同一 child 继续发送", async () => {
  const sends: string[] = [];
  const reads: ((reply: Response) => void)[] = [];
  let holdReads = false;
  installSessionCatalogFetchMock(({ path, init }) => {
    if (path.endsWith("/messages")) {
      sends.push(JSON.parse(String(init?.body)).message.content);
      holdReads = true;
      return response({ message_id: `message-${sends.length}`, job_id: `job-${sends.length}`, turn_id: `turn-${sends.length}`, thread_id: CHILDREN[0]!.thread_id, status: "queued", dispatch: {} });
    }
    return holdReads ? new Promise<Response>((resolve) => { reads.push(resolve); }) : response(history(CHILDREN[0]!.thread_id));
  });
  act(() => { renderer = create(<Harness />); });
  act(() => current.selectThread(CHILDREN[0]!.thread_id));
  await until(() => current.snapshot.history !== null);
  act(() => current.setDraft("第一条"));
  let first: Promise<void>;
  let second: Promise<void> | undefined;
  act(() => { first = current.send(); });
  try {
    await until(() => current.snapshot.accepted !== null && !current.snapshot.sending && reads.length > 0);
    act(() => current.setDraft("第二条"));
    act(() => { second = current.send(); });
    await until(() => sends.length === 2);
    expect(sends).toEqual(["第一条", "第二条"]);
  } finally {
    holdReads = false;
    await act(async () => {
      for (const resolve of reads) resolve(response(history(CHILDREN[0]!.thread_id)));
      await Promise.all([first!, second]);
    });
  }
});

test("旧发送失败的历史刷新结算不能释放后继仍待回执的发送锁", async () => {
  let sends = 0;
  let firstRead: ((reply: Response) => void) | null = null;
  let secondSend: ((reply: Response) => void) | null = null;
  let holdRead = false;
  installSessionCatalogFetchMock(({ path }) => {
    if (path.endsWith("/messages")) {
      sends += 1;
      if (sends === 1) {
        holdRead = true;
        return Response.json({ detail: "首条发送失败" }, { status: 409 });
      }
      return new Promise<Response>((resolve) => { secondSend = resolve; });
    }
    return holdRead ? new Promise<Response>((resolve) => { firstRead = resolve; }) : response(history(CHILDREN[0]!.thread_id));
  });
  act(() => { renderer = create(<Harness />); });
  act(() => current.selectThread(CHILDREN[0]!.thread_id));
  await until(() => current.snapshot.history !== null);
  act(() => current.setDraft("第一条"));
  let first: Promise<void>;
  let second: Promise<void> | undefined;
  act(() => { first = current.send(); });
  try {
    await until(() => firstRead !== null && !current.snapshot.sending);
    act(() => current.setDraft("第二条"));
    act(() => { second = current.send(); });
    await until(() => secondSend !== null && current.snapshot.sending);
    holdRead = false;
    await act(async () => {
      firstRead!(response(history(CHILDREN[0]!.thread_id)));
      await first!;
    });
    expect(current.snapshot.sending).toBe(true);
    await act(async () => { await current.send(); });
    expect(sends).toBe(2);
  } finally {
    holdRead = false;
    await act(async () => {
      firstRead?.(response(history(CHILDREN[0]!.thread_id)));
      secondSend?.(response({ message_id: "message-2", job_id: "job-2", turn_id: "turn-2", thread_id: CHILDREN[0]!.thread_id, status: "queued", dispatch: {} }));
      await Promise.all([first!, second]);
    });
  }
});
