import { afterEach, describe, expect, jest, test } from "bun:test";
import { getLLMRequestLogs, interruptSession, listMessages, replayMessageTurn, sendUserMessage } from "./sessionMessages";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import {
  installSessionApiFetchMock,
  unwrapSessionApiFetch,
} from "./sessionApiFetchMock";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port), origin: `http://127.0.0.1:${port}` },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function installFetchMock(respond: () => Response): void {
  installSessionApiFetchMock(() => respond(), { credentialToken: "messages-token" });
}

afterEach(() => {
  jest.useRealTimers();
  unwrapSessionApiFetch();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("会话消息列表 API 的 CursorPage 契约", () => {
  test("合法空页：items 为空数组正常返回", async () => {
    installFetchMock(() => Response.json({
      data: { items: [], next_cursor: null, has_more: false },
      request_id: "req-messages-empty",
    }));

    await expect(listMessages(48_308, "ses_x", "workspace-1")).resolves.toEqual({
      items: [],
      next_cursor: null,
      has_more: false,
    });
  });

  test("items 为数字：响亮失败而不是静默收敛为空页", async () => {
    installFetchMock(() => Response.json({
      data: { items: 5, has_more: true },
      request_id: "req-messages-broken-items",
    }));

    await expect(listMessages(48_309, "ses_x", "workspace-1")).rejects
      .toThrow("会话消息列表响应 items 必须是数组，实际为 number");
  });
});

describe("中断请求只作用于明确的 Thread", () => {
  const result = { session_id: "ses_interrupt", thread_id: "thr_main", job_id: "job_interrupt", status: "cancelling", phase: "tool", interrupt_request_id: "intr_exact" };

  test("传递精确 Thread 并保留后端完整回执", async () => {
    const requests: string[] = [];
    installSessionApiFetchMock(({ url, method }) => {
      expect(method).toBe("POST");
      requests.push(url);
      return Response.json({ data: result, request_id: "req-interrupt" });
    });
    expect(await interruptSession(48_311, result.session_id, result.thread_id, "workspace-1")).toEqual(result);
    expect(requests).toHaveLength(1);
    expect(new URL(requests[0], "http://localhost").searchParams.get("thread_id")).toBe(result.thread_id);
  });

  test.each([
    { ...result, session_id: "ses_other" },
    { ...result, thread_id: "thr_child" },
    { ...result, thread_id: undefined },
  ])("拒绝错位或缺失的回执身份 %j", async (response) => {
    installFetchMock(() => Response.json({ data: response, request_id: "req-interrupt-bad-owner" }));
    await expect(interruptSession(48_311, result.session_id, result.thread_id, "workspace-1")).rejects.toThrow("中断响应归属不匹配");
  });
});

describe("请求日志读取必须带上限，不能无限等待", () => {
  test("后端一直不响应时按全局默认上限超时并响亮报错", async () => {
    const port = 48_310;
    installWindow(port);
    jest.useFakeTimers();
    let requestSignal: AbortSignal | null = null;
    let markRequestStarted: (() => void) | null = null;
    const requestStarted = new Promise<void>((resolve) => {
      markRequestStarted = resolve;
    });
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input), `http://127.0.0.1:${port}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "llm-log-token" } });
        }
        requestSignal = init?.signal ?? null;
        markRequestStarted!();
        // 模拟后端卡死：只有在被 abort 时才结束，绝不自己返回。
        return await new Promise<Response>((_, reject) => {
          const abort = () => reject(
            requestSignal?.reason ?? new DOMException("aborted", "AbortError"),
          );
          if (requestSignal?.aborted) {
            abort();
            return;
          }
          requestSignal?.addEventListener("abort", abort, { once: true });
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    const pending = getLLMRequestLogs(port, "ses_timeout", "workspace-1");
    // 让凭据与业务请求走到 fetch，然后推进时间越过超时上限。
    await requestStarted;
    jest.advanceTimersByTime(15_000);
    await expect(pending).rejects.toThrow("请求超时");
    // 超时必须是真实的中止，而不是把请求晾在一边继续等。
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(true);
  });
});

describe("发送与重放统一消费真实准入身份", () => {
  const accepted = {
    message_id: "message-accepted",
    job_id: "job-accepted",
    turn_id: "turn-accepted",
    thread_id: "thread-accepted",
    status: "queued",
    dispatch: {
      session_id: "session-accepted",
      thread_id: "thread-accepted",
      job_id: "job-accepted",
      job_status: "queued",
      queued_jobs_ahead: 0,
      queued_job_count: 1,
      pending_job_count: 1,
    },
    session_id: "session-accepted",
    action: "regenerate",
    replaced_message_id: "message-original",
    removed_message_count: 1,
    workspace_changes_reverted: false,
    notice: "已准入重放",
  } as const;
  const actions = [
    { name: "发送消息", run: () => sendUserMessage(48_311, "session-accepted", "实际用户输入") },
    { name: "重放消息", run: () => replayMessageTurn(48_311, "session-accepted", "message-original", { action: "regenerate" }) },
  ];
  for (const action of actions) {
    test(`${action.name}保留后端独立的 message/Job/Turn/thread`, async () => {
      installFetchMock(() => Response.json({ data: accepted, request_id: "req-admission-valid" }));
      await expect(action.run()).resolves.toEqual(accepted);
    });
    for (const field of ["message_id", "job_id", "turn_id", "thread_id"] as const) {
      test(`${action.name}拒绝缺失 ${field}`, async () => {
        const data: Record<string, unknown> = { ...accepted };
        delete data[field];
        installFetchMock(() => Response.json({ data, request_id: "req-admission-missing" }));
        await expect(action.run()).rejects.toThrow(`${action.name}响应缺少${field === "turn_id" ? "独立的 turn_id" : ` ${field}`}`);
      });
    }
    test(`${action.name}拒绝以 Job 代替 Turn`, async () => {
      installFetchMock(() => Response.json({ data: { ...accepted, turn_id: accepted.job_id }, request_id: "req-admission-alias" }));
      await expect(action.run()).rejects.toThrow(`${action.name}响应缺少独立的 turn_id`);
    });
  }
});
