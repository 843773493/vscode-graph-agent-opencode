import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installSessionCatalogFetchMock, unwrapSessionCatalogFetch } from "../../api/session/sessionApiFetchMock";
import type { ChildThreadSummary } from "../../types/backend";
import { useChildThreadConversation } from "./useChildThreadConversation";

const SESSION = "ses_019fca64740070008000000000000001";
const CHILDREN: ChildThreadSummary[] = [1, 2].map((index) => ({
  thread_id: `thr_019fca6474007000800000000000000${index}`,
  created_at: "2026-10-08T00:00:00Z", status: "completed",
  report_execution_id: `execution-${index}`, report_status: "completed",
}));
let current: ReturnType<typeof useChildThreadConversation>;
let renderer: ReactTestRenderer | null = null;
const response = (data: unknown) => Response.json({ code: 0, message: "ok", request_id: "req-child", data });
const history = (threadId: string) => ({ thread_id: threadId, items: [], internal_executions: [], projection_epoch: 1, has_before: false });

function Harness() {
  current = useChildThreadConversation({ apiPort: 49199, workspaceId: "workspace-a", sessionId: SESSION, threads: CHILDREN, enabled: true });
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
  expect(requests[requests.length - 1]!.body).toEqual({ direction: "tail", execution_id: "execution-1", include: ["user", "text", "final_response", "internal"] });
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
    completeSend!(response({ message_id: "message-a", job_id: "job-a", thread_id: CHILDREN[0]!.thread_id, status: "queued", dispatch: {} }));
    await pending!;
  });
  expect(sends).toEqual([{ path: `/api/v1/sessions/${SESSION}/threads/${CHILDREN[0]!.thread_id}/messages`, workspace: "workspace-a", content: "只发给 child A" }]);
  expect(current.selectedThread?.thread_id).toBe(CHILDREN[1]!.thread_id);
  expect(current.snapshot.history?.thread_id).toBe(CHILDREN[1]!.thread_id);
  expect(current.snapshot.accepted).toBeNull();
  expect(current.snapshot.loading).toBe(false);
});
