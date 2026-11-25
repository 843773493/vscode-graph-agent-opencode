import { afterEach, describe, expect, jest, test } from "bun:test";
import "fake-indexeddb/auto";
import {
  getLLMRequestLogs,
  getSessionAttachmentTextPreview,
  listMessages,
  replayMessageTurn,
  sendUserMessage,
  reconcileMessageSubmissions,
  uploadSessionAttachment,
  discardSessionAttachment,
} from "./sessionMessages";
import { gatewayWorkspaceKey } from "../workspaceIdentity";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import {
  installSessionApiFetchMock as installSessionApiFetch,
  unwrapSessionApiFetch,
} from "./sessionApiFetchMock";
import { loadMessageSubmissions, releaseMessageSubmission } from "../../state/messageSubmissions/storage";

const OWNER = { gatewayId: "gateway-messages", workspaceId: "00000000-0000-4000-8000-000000000065" };
const WORKSPACE_KEY = gatewayWorkspaceKey(OWNER);
const MESSAGE_OPERATION_ID = "op_0199e600000070008000000000000001";
const businessRequests: string[] = [];

function installSessionApiFetchMock(...[handler, options]: Parameters<typeof installSessionApiFetch>): void {
  installSessionApiFetch((request) => {
    expect(request.path.startsWith("/api/v1/sessions/")).toBe(true);
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNER.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNER.workspaceId);
    businessRequests.push(request.path);
    if (request.path.includes("/message-submissions/")) {
      return Response.json({ data: {
        session_id: "session-accepted",
        thread_id: "thread-accepted",
        operation_id: decodeURIComponent(request.path.split("/").at(-1)!),
        status: "unknown",
        accepted: null,
      }, request_id: "req-message-submission-unknown" });
    }
    return handler(request);
  }, options);
}
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

afterEach(async () => {
  jest.useRealTimers();
  unwrapSessionApiFetch();
  const count = businessRequests.length;
  businessRequests.length = 0;
  expect(count).toBeGreaterThan(0);
  const owner = { workspaceKey: WORKSPACE_KEY, sessionId: "session-accepted", threadId: "thread-accepted" };
  for (const record of await loadMessageSubmissions(owner)) await releaseMessageSubmission(record);
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("会话消息列表 API 的 CursorPage 契约", () => {
  test("合法空页：items 为空数组正常返回", async () => {
    installFetchMock(() => Response.json({
      data: { items: [], next_cursor: null, has_more: false },
      request_id: "req-messages-empty",
    }));

    await expect(listMessages(48_308, "ses_x", WORKSPACE_KEY)).resolves.toEqual({
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

    await expect(listMessages(48_309, "ses_x", WORKSPACE_KEY)).rejects
      .toThrow("会话消息列表响应 items 必须是数组，实际为 number");
  });
});

describe("会话附件文本预览按需读取", () => {
  test("超过预览上限时取消剩余响应体，并正确处理跨块 UTF-8 字符", async () => {
    const encoder = new TextEncoder();
    const chunks = [
      encoder.encode("abc"),
      encoder.encode("界").subarray(0, 1),
      encoder.encode("界").subarray(1),
      encoder.encode("未读取的尾部"),
    ];
    let pullCount = 0;
    let cancelled = false;
    installSessionApiFetchMock(({ path, url }) => {
      expect(path).toBe("/api/v1/sessions/ses_preview/attachments/content");
      const query = new URL(url).searchParams;
      expect(query.get("file_id")).toBe("file_preview");
      expect(query.get("variant")).toBe("original");
      return new Response(new ReadableStream<Uint8Array>({
        pull(controller) {
          pullCount += 1;
          const chunk = chunks.shift();
          if (chunk) controller.enqueue(chunk);
          else controller.close();
        },
        cancel() {
          cancelled = true;
        },
      }, { highWaterMark: 0 }));
    });

    await expect(getSessionAttachmentTextPreview(
      48_315,
      "ses_preview",
      "file_preview",
      WORKSPACE_KEY,
      { maxCodeUnits: 3 },
    )).resolves.toEqual({ text: "abc", truncated: true });
    expect(pullCount).toBe(3);
    expect(cancelled).toBe(true);
  });

  test("正好达到上限且正文结束时不标记截断", async () => {
    installSessionApiFetchMock(() => new Response(new TextEncoder().encode("你好")));

    await expect(getSessionAttachmentTextPreview(
      48_316,
      "ses_preview",
      "file_preview",
      WORKSPACE_KEY,
      { maxCodeUnits: 2 },
    )).resolves.toEqual({ text: "你好", truncated: false });
  });

  test.each([0, 1, 2, 3, 4])("按字节分块时，长度 %i 不截断代理对", async maxCodeUnits => {
    const bytes = new TextEncoder().encode("a🙂尾");
    let index = 0;
    installSessionApiFetchMock(() => new Response(new ReadableStream<Uint8Array>({
      pull(controller) {
        if (index === bytes.length) controller.close();
        else controller.enqueue(bytes.slice(index, ++index));
      },
    })));
    const expected = ["", "a", "a", "a🙂", "a🙂尾"][maxCodeUnits];
    await expect(getSessionAttachmentTextPreview(48_317, "ses_preview", "file_preview", WORKSPACE_KEY,
      { maxCodeUnits })).resolves.toEqual({ text: expected, truncated: maxCodeUnits < 4 });
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
    installSessionApiFetchMock(async ({ path, init }) => {
      expect(path).toBe("/api/v1/sessions/ses_timeout/llm-request-logs");
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
    }, { credentialToken: "llm-log-token" });

    const pending = getLLMRequestLogs(port, "ses_timeout", WORKSPACE_KEY);
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
    operation_id: MESSAGE_OPERATION_ID,
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
      queue_snapshot_version: 11,
    },
    session_id: "session-accepted",
    action: "regenerate",
    replaced_message_id: "message-original",
    removed_message_count: 1,
    workspace_changes_reverted: false,
    notice: "已准入重放",
  } as const;
  test.each(["accepted", "unknown"] as const)("恢复查询避让本页面的在途提交，结束后恢复原查询：%s", async outcome => {
    const owner = { workspaceKey: WORKSPACE_KEY, sessionId: "session-accepted", threadId: "thread-accepted" };
    let finish!: (response: Response) => void;
    let started!: () => void;
    const postStarted = new Promise<void>(resolve => { started = resolve; });
    const response = new Promise<Response>(resolve => { finish = resolve; });
    installSessionApiFetchMock(() => { started(); return response; });
    const sending = sendUserMessage(48_311, owner.sessionId, "在途输入", undefined, [],
      WORKSPACE_KEY, "after_turn", owner.threadId, MESSAGE_OPERATION_ID)
      .then(value => ({ value, error: null }), error => ({ value: null, error }));
    try {
      await postStarted;
      const results = await reconcileMessageSubmissions(48_311, owner);
      expect(results).toHaveLength(1);
      expect(results[0]?.accepted).toBeNull();
      expect(businessRequests.filter(path => path.includes("/message-submissions/"))).toEqual([]);
    } finally {
      finish(outcome === "accepted"
        ? Response.json({ data: accepted, request_id: "req-active-send" })
        : Response.json({ detail: "响应丢失" }, { status: 503 }));
    }
    const result = await sending;
    if (outcome === "accepted") {
      expect(result.value).toEqual(accepted);
      expect(await loadMessageSubmissions(owner)).toEqual([]);
      expect(businessRequests).toHaveLength(1);
    } else {
      expect(result.error).toBeInstanceOf(Error);
      expect(await loadMessageSubmissions(owner)).toHaveLength(1);
      expect(businessRequests.filter(path => path.includes("/message-submissions/"))).toHaveLength(1);
      await reconcileMessageSubmissions(48_311, owner);
      expect(businessRequests.filter(path => path.includes("/message-submissions/"))).toHaveLength(2);
    }
  });
  test("预上传发送原始字节，消息和重试快照只保存附件引用", async () => {
    const attachment = { file_id: "boxteam://gateway-test/workspaces/ws/attachments/att_test/original/content",
      directory_uri: "boxteam://gateway-test/workspaces/ws/attachments/att_test", name: "报告.txt", content_type: "text/plain" };
    const file = new File(["原始文件 Ω"], attachment.name, { type: "text/plain" });
    const requests: string[] = [];
    installSessionApiFetchMock(async ({ path, method, init }) => {
      requests.push(`${method} ${path}`);
      if (path.endsWith("/attachments")) {
        if (method === "POST") {
          expect(init?.body).toBe(file);
          expect(new Headers(init?.headers).get("Content-Type")).toBe(file.type);
          return Response.json({ data: attachment, request_id: "req-upload" });
        }
        expect(method).toBe("DELETE");
        return Response.json({ data: null, request_id: "req-discard" });
      }
      expect(path.endsWith("/messages")).toBe(true);
      const payload = JSON.parse(String(init?.body));
      expect(payload.message.attachments).toEqual([attachment]);
      expect(String(init?.body)).not.toContain("data:");
      const [pending] = await loadMessageSubmissions({ workspaceKey: WORKSPACE_KEY, sessionId: "session-accepted", threadId: "thread-accepted" });
      expect(pending.request.message.attachments).toEqual([attachment]);
      return Response.json({ data: accepted, request_id: "req-send" });
    });
    const uploaded = await uploadSessionAttachment(48_311, "session-accepted", "thread-accepted", WORKSPACE_KEY, file, file.name, file.type);
    await sendUserMessage(48_311, "session-accepted", "读取附件", undefined, [uploaded], WORKSPACE_KEY, "after_turn", "thread-accepted", MESSAGE_OPERATION_ID);
    await discardSessionAttachment(48_311, "session-accepted", "thread-accepted", WORKSPACE_KEY, uploaded.file_id);
    expect(requests.map(request => request.split(" ")[0])).toEqual(["POST", "POST", "DELETE"]);
  });
  const actions = [
    { name: "发送消息", run: () => sendUserMessage(48_311, "session-accepted", "实际用户输入", undefined, [],
      WORKSPACE_KEY, "after_turn", "thread-accepted", MESSAGE_OPERATION_ID) },
    { name: "重放消息", run: () => replayMessageTurn(48_311, "session-accepted", "message-original", { action: "regenerate" }, WORKSPACE_KEY) },
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
    for (const [field, value] of [
      ["job_id", "job_other"],
      ["thread_id", "thread_other"],
      ["job_status", "running"],
      ["session_id", ""],
      ["queue_snapshot_version", -1],
      ["queue_snapshot_version", 1.5],
    ] as const) {
      test(`${action.name}拒绝 dispatch 的无效 ${field}`, async () => {
        const data = {
          ...accepted,
          dispatch: { ...accepted.dispatch, [field]: value },
        };
        installFetchMock(() => Response.json({ data, request_id: "req-admission-dispatch-owner" }));
        await expect(action.run()).rejects.toThrow(`${action.name}响应缺少对应 Job／Thread 的权威队列水位`);
      });
    }
    test(`${action.name}拒绝以 Job 代替 Turn`, async () => {
      installFetchMock(() => Response.json({ data: { ...accepted, turn_id: accepted.job_id }, request_id: "req-admission-alias" }));
      await expect(action.run()).rejects.toThrow(`${action.name}响应缺少独立的 turn_id`);
    });
  }

  test("发送请求与接受回执保留同一个 operation_id 及精确 owner", async () => {
    let submittedOperationId: string | undefined;
    installSessionApiFetchMock(({ path, method, init }) => {
      expect(method).toBe("POST");
      expect(path).toBe("/api/v1/sessions/session-accepted/threads/thread-accepted/messages");
      const request = JSON.parse(String(init?.body)) as { operation_id: string };
      submittedOperationId = request.operation_id;
      return Response.json({
        data: { ...accepted, operation_id: submittedOperationId },
        request_id: "req-message-operation-id",
      });
    }, { credentialToken: "messages-token" });

    const result = await sendUserMessage(48_311, "session-accepted", "实际用户输入", undefined, [],
      WORKSPACE_KEY, "after_turn", "thread-accepted", MESSAGE_OPERATION_ID);
    expect(submittedOperationId).toBe(MESSAGE_OPERATION_ID);
    expect(result.operation_id).toBe(submittedOperationId);
  });
});
