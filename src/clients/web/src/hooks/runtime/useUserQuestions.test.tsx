import { useRealtimeSocket } from "../../../../../../tests/harness/js/realtime-socket";
useRealtimeSocket();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useUserQuestions } from "./useUserQuestions";
import type { UserQuestionOwner, UserQuestionSummaryDTO } from "../../api/userQuestions";

const originalFetch = globalThis.fetch;
let renderer: ReactTestRenderer;
afterEach(() => { act(() => renderer?.unmount()); globalThis.fetch = originalFetch; });

test("折叠只订阅摘要，展开分页与逐条删除，切换 Thread 废弃旧请求", async () => {
  const owner: UserQuestionOwner = { port: 49617, sessionId: "ses_questions", threadId: "thr_main",
    workspaceKey: gatewayWorkspaceKey({ gatewayId: "gateway_00000000000040008000000000000001", workspaceId: "00000000-0000-4000-8000-000000000001" }) };
  let expanded = false, target = owner;
  let value!: ReturnType<typeof useUserQuestions>;
  const errors: string[] = [], requests: string[] = [];
  const report = (message: string) => errors.push(message);
  const streams = new Map<string, (summary: UserQuestionSummaryDTO) => void>();
  let rev = 1, count = 2;
  const summary = () => ({ session_id: owner.sessionId, thread_id: owner.threadId, revision: rev, count });
  const json = (data: unknown) => Response.json({ code: 0, message: "ok", request_id: "question-test", data });
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/local-credential")) return json({ token: "test-token" });
    requests.push(`${init?.method ?? "GET"} ${url.pathname}${url.search}`);
    if (url.pathname.endsWith("/questions/events")) {
      const threadId = url.pathname.split('/').at(-3)!;
      return new Response(new ReadableStream<Uint8Array>({ start(controller) {
        streams.set(threadId, data => controller.enqueue(new TextEncoder().encode(`event: changed\ndata: ${JSON.stringify(data)}\n\n`)));
      } }), { headers: { "Content-Type": "text/event-stream" } });
    }
    if (init?.method === "DELETE") { count = 1; rev++; return json(summary()); }
    const older = url.searchParams.has("before");
    return json({ ...summary(), items: [{ question_id: older ? "older" : "newer", sequence: older ? 1 : 2,
      markdown: "**请稍后阅读**", turn_id: null, created_at: "2026-11-17T00:00:00Z" }], next_before: older ? null : 2 });
  }, { preconnect: originalFetch.preconnect });
  function Harness() { value = useUserQuestions(target, expanded, report); return null; }
  await act(async () => { renderer = create(<Harness />); });
  const firstStream = [...streams.values()][0]!;
  await act(async () => firstStream(summary()));
  expect(value.count).toBe(2);
  expect(value.items).toEqual([]);
  expect(requests).toEqual([`GET /api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/questions/events`]);
  await act(async () => { expanded = true; renderer.update(<Harness />); });
  expect(value.items.map(item => item.question_id)).toEqual(["newer"]);
  await act(async () => value.loadMore());
  expect(value.items.map(item => item.question_id)).toEqual(["newer", "older"]);
  expect(value.hasMore).toBe(false);
  await act(async () => value.remove("older"));
  expect(value.count).toBe(1);
  expect(requests.filter(path => path.startsWith("DELETE"))).toHaveLength(1);
  await act(async () => { target = { ...owner, threadId: "thr_other" }; renderer.update(<Harness />); });
  expect(value.count).toBe(0);
  expect(value.items).toEqual([]);
  expect(errors).toEqual([]);
});
