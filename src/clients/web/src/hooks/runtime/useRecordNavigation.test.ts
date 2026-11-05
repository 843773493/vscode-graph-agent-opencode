import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { HttpRequestError } from "../../api/http";
import * as sessionMessages from "../../api/session/sessionMessages";
import type { LocatedLLMRequestLog, LocatedTrace } from "../../api/session/sessionMessages";
import type { LLMRequestLogMatchDTO, TraceMatchDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { useRequestLogNavigation } from "./useRequestLogNavigation";
import { useTraceNavigation } from "./useTraceNavigation";

const PORT = 49_607;
const WORKSPACE_ID = "workspace_record_navigation";
const SESSION_ID = "session_record_navigation";
const THREAD_ID = "thread_record_navigation";
type Kind = "request" | "trace";

function hit(id: string) {
  const requestMatch = {
    reference: {
      session_id: SESSION_ID, file_name: `${id}.json`, content_version: `request-${id}`,
      start_codepoint: 0, end_codepoint: 3,
    },
    timestamp: 1_793_836_800_000, preview: id, preview_start_codepoint: 0,
  } satisfies LLMRequestLogMatchDTO;
  const traceMatch = {
    reference: {
      session_id: SESSION_ID, thread_id: THREAD_ID, event_id: `event-${id}`,
      line_start: 1, line_end: 1, content_version: `trace-${id}`,
      start_codepoint: 0, end_codepoint: 3,
    },
    event_type: "model_call", timestamp: "2026-11-05T00:00:00Z",
    preview: id, preview_start_codepoint: 0,
  } satisfies TraceMatchDTO;
  const requestLocation = {
    reference: requestMatch.reference,
    record: {
      session_id: SESSION_ID, file_name: requestMatch.reference.file_name,
      timestamp: requestMatch.timestamp, request: { content: id }, response: {}, upstream: {},
    },
    text: `请求原记录 ${id}`,
  } satisfies LocatedLLMRequestLog;
  const traceLocation = {
    reference: traceMatch.reference,
    record: {
      session_id: SESSION_ID, thread_id: THREAD_ID, event_id: traceMatch.reference.event_id,
      job_id: `job-${id}`, type: traceMatch.event_type, timestamp: traceMatch.timestamp,
      phase: "complete", title: id, content: `Trace 原记录 ${id}`,
    },
    text: `Trace 原记录 ${id}`,
  } satisfies LocatedTrace;
  return {
    request: { match: requestMatch, location: requestLocation },
    trace: { match: traceMatch, location: traceLocation },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function pending() {
  return {
    request: deferred<LocatedLLMRequestLog>(),
    trace: deferred<LocatedTrace>(),
  };
}

const restores: Array<() => void> = [];
function readers() {
  const request = spyOn(sessionMessages, "locateLLMRequestLog");
  const trace = spyOn(sessionMessages, "locateTrace");
  request.mockImplementation(() => { throw new Error("未设置请求日志定位响应"); });
  trace.mockImplementation(() => { throw new Error("未设置 Trace 定位响应"); });
  restores.push(() => request.mockRestore(), () => trace.mockRestore());
  return { request, trace };
}

let requestState: ReturnType<typeof useRequestLogNavigation>;
let traceState: ReturnType<typeof useTraceNavigation>;
let renderer: ReactTestRenderer | undefined;

function Harness({ kind, entry }: { kind: Kind; entry?: ReturnType<typeof hit> }): null {
  requestState = useRequestLogNavigation(PORT, WORKSPACE_ID, SESSION_ID,
    kind === "request" ? entry?.request.match : undefined);
  traceState = useTraceNavigation(PORT, WORKSPACE_ID, SESSION_ID, THREAD_ID,
    kind === "trace" ? entry?.trace.match : undefined);
  return null;
}

async function render(kind: Kind, entry?: ReturnType<typeof hit>): Promise<void> {
  await act(async () => {
    const element = createElement(Harness, { kind, entry });
    if (renderer) renderer.update(element);
    else renderer = create(element);
  });
}

function current(kind: Kind) {
  return kind === "request" ? requestState : traceState;
}

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  requestState = undefined;
  traceState = undefined;
  restores.splice(0).forEach(restore => restore());
});

for (const kind of ["request", "trace"] as const) {
  describe(`${kind} 原记录定位`, () => {
    test.each(["成功", "失败"] as const)("慢旧命中迟到%s不能覆盖新命中", async (outcome) => {
      const reader = readers();
      const old = pending();
      const first = hit("old");
      const second = hit("new");
      reader.request.mockReturnValueOnce(old.request.promise).mockResolvedValueOnce(second.request.location);
      reader.trace.mockReturnValueOnce(old.trace.promise).mockResolvedValueOnce(second.trace.location);

      await render(kind, first);
      expect(current(kind)).toEqual(kind === "request"
        ? { match: first.request.match } : { match: first.trace.match });
      const oldSignal = reader[kind].mock.calls[0]?.[3];
      expect(oldSignal).toBeInstanceOf(AbortSignal);
      expect(oldSignal?.aborted).toBe(false);

      await render(kind, second);
      expect(oldSignal?.aborted).toBe(true);
      expect(reader[kind]).toHaveBeenCalledTimes(2);
      expect(reader[kind].mock.calls[1]).toEqual(kind === "request"
        ? [PORT, second.request.match.reference, WORKSPACE_ID, expect.any(AbortSignal)]
        : [PORT, second.trace.match.reference, WORKSPACE_ID, expect.any(AbortSignal)]);
      expect(reader[kind].mock.calls[1]?.[3]?.aborted).toBe(false);
      const accepted = current(kind);
      expect(accepted?.match).toBe(second[kind].match);
      expect(accepted?.location).toBe(second[kind].location);
      expect(accepted?.error).toBeUndefined();

      // 边界桩故意不响应 abort，让真实 hook 处理取消后仍结算的旧请求。
      await act(async () => {
        if (kind === "request") {
          if (outcome === "成功") old.request.resolve(first.request.location);
          else old.request.reject(new Error("旧请求日志迟到失败"));
        } else {
          if (outcome === "成功") old.trace.resolve(first.trace.location);
          else old.trace.reject(new Error("旧 Trace 迟到失败"));
        }
      });
      expect(current(kind)).toBe(accepted);
    });

    test("关闭定位和卸载都会取消在途请求", async () => {
      const reader = readers();
      const closed = pending();
      const unmounted = pending();
      const entry = hit("cancel");
      reader.request.mockReturnValueOnce(closed.request.promise).mockReturnValueOnce(unmounted.request.promise);
      reader.trace.mockReturnValueOnce(closed.trace.promise).mockReturnValueOnce(unmounted.trace.promise);

      await render(kind, entry);
      const closeSignal = reader[kind].mock.calls[0]?.[3];
      expect(closeSignal?.aborted).toBe(false);
      await render(kind);
      expect(closeSignal?.aborted).toBe(true);
      expect(current(kind)).toBeUndefined();
      await act(async () => {
        if (kind === "request") closed.request.resolve(entry.request.location);
        else closed.trace.resolve(entry.trace.location);
      });
      expect(current(kind)).toBeUndefined();

      await render(kind, entry);
      const unmountSignal = reader[kind].mock.calls[1]?.[3];
      expect(unmountSignal?.aborted).toBe(false);
      act(() => renderer?.unmount());
      renderer = undefined;
      expect(unmountSignal?.aborted).toBe(true);
      await act(async () => {
        if (kind === "request") unmounted.request.reject(new Error("卸载后请求日志失败"));
        else unmounted.trace.reject(new Error("卸载后 Trace 失败"));
      });
      expect(reader[kind]).toHaveBeenCalledTimes(2);
    });

    test("Session 归属错误在定位 API 前拒绝", async () => {
      const reader = readers();
      const entry = hit("wrong-session");
      entry[kind].match.reference.session_id = "another_session";
      await render(kind, entry);
      expect(reader.request).not.toHaveBeenCalled();
      expect(reader.trace).not.toHaveBeenCalled();
      expect(current(kind)).toEqual(kind === "request"
        ? { match: entry.request.match, error: "请求日志命中与目标 Session 归属不一致" }
        : { match: entry.trace.match, error: "Trace 命中与目标 Session／Thread 归属不一致" });
    });

    test("定位 API 返回记录已删除时保留原始原因", async () => {
      const reader = readers();
      const entry = hit("deleted");
      const error = new HttpRequestError(409, "Conflict", { message: "原记录已删除：内容版本不再存在" },
        `/api/v1/sessions/${SESSION_ID}/${kind === "request" ? "llm-request-logs" : "traces"}/locate`, "request-deleted-record");
      reader.request.mockRejectedValueOnce(error);
      reader.trace.mockRejectedValueOnce(error);
      await render(kind, entry);
      expect(reader[kind]).toHaveBeenCalledTimes(1);
      expect(reader[kind]).toHaveBeenCalledWith(PORT, entry[kind].match.reference, WORKSPACE_ID, expect.any(AbortSignal));
      expect(current(kind)).toEqual(kind === "request"
        ? { match: entry.request.match, error: error.message }
        : { match: entry.trace.match, error: error.message });
    });
  });
}

test("Trace Thread 归属错误在定位 API 前拒绝", async () => {
  const reader = readers();
  const entry = hit("wrong-thread");
  entry.trace.match.reference.thread_id = "another_thread";
  await render("trace", entry);
  expect(reader.request).not.toHaveBeenCalled();
  expect(reader.trace).not.toHaveBeenCalled();
  expect(traceState).toEqual({ match: entry.trace.match, error: "Trace 命中与目标 Session／Thread 归属不一致" });
});
