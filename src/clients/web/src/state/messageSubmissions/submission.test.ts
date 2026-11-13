import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "../../api/session/sessionApiFetchMock";
import { MessageSubmissionRejectedError, MessageSubmissionUnknownError, reconcileMessageSubmissions, sendUserMessage, submitFrozenMessage } from "../../api/session/sessionMessages";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { MessageRunAccepted, MessageRunRequest } from "../../types/backend";
import { installWindowWithTimers, restoreGlobalDescriptor } from "../../tests/testGlobals";
import { openBrowserViewDatabase, readBrowserView, updateBrowserView } from "../browserViewStore";
import { readComposerDraft, writeComposerDraft } from "../composerDrafts/storage";
import { freezeMessageSubmission, loadMessageSubmissions, messageSubmissionKey, rejectMessageSubmission,
  releaseMessageSubmission, resubmitRejectedMessage, type MessageSubmissionOwner } from "./storage";

const workspaceKey = gatewayWorkspaceKey({ gatewayId: "gateway-submission", workspaceId: "00000000-0000-4000-8000-000000000077" });
const PORT = 48677;
let owner: MessageSubmissionOwner;
let restoreWindow: () => void;
const originalFileReader = Object.getOwnPropertyDescriptor(globalThis, "FileReader");

beforeEach(() => {
  owner = { workspaceKey, sessionId: "ses_submission", threadId: `thr_${crypto.randomUUID()}` };
  restoreWindow = installWindowWithTimers({ port: String(PORT), origin: `http://127.0.0.1:${PORT}` });
});
afterEach(async () => {
  unwrapSessionApiFetch(); restoreWindow(); restoreGlobalDescriptor("FileReader", originalFileReader);
  await updateBrowserView(messageSubmissionKey(owner), () => undefined);
});

function input(): MessageRunRequest {
  return { operation_id: `op_${crypto.randomUUID()}`, message: { role: "user", content: "输入A", attachments: [
    { file_id: "inline-a", name: "a.txt", content_type: "text/plain", data_url: "data:text/plain;base64,QQ==" },
    { file_id: "inline-b", name: "b.txt", content_type: "text/plain", data_url: "data:text/plain;base64,Qg==" },
  ], metadata: {} }, run: { mode: "single_agent", agent_id: "default", response_mode: "stream", async: true, delivery_policy: "after_tool_result" } };
}
function receipt(operationId: string): MessageRunAccepted {
  return { operation_id: operationId, message_id: "msg_canonical", turn_id: "turn_canonical", job_id: "job_canonical",
    thread_id: owner.threadId, status: "queued", dispatch: {
      session_id: owner.sessionId, thread_id: owner.threadId, job_id: "job_canonical", job_status: "queued",
      queued_jobs_ahead: 0, queued_job_count: 1, pending_job_count: 1, queue_snapshot_version: 4,
    } };
}
function response(data: unknown) { return Response.json({ code: 0, request_id: "req_submission", data }); }
function unknown(operationId: string) { return response({ session_id: owner.sessionId, thread_id: owner.threadId, operation_id: operationId, status: "unknown", accepted: null }); }

test("首次POST之前完整冻结输入，草稿B与清空不改变A及有序附件", async () => {
  const request = input();
  const draftKey = `submission-draft-${owner.threadId}`;
  await writeComposerDraft(draftKey, request.message.content);
  let sent: MessageRunRequest | undefined;
  installSessionApiFetchMock(async ({ method, init }) => {
    expect(method).toBe("POST");
    sent = JSON.parse(String(init?.body)) as MessageRunRequest;
    const records = await loadMessageSubmissions(owner);
    expect(records).toHaveLength(1);
    expect(records[0]!.request).toEqual(sent);
    await writeComposerDraft(draftKey, "草稿B");
    await writeComposerDraft(draftKey, "");
    return response(receipt(sent.operation_id));
  });
  await sendUserMessage(PORT, owner.sessionId, request.message.content, "default", request.message.attachments,
    workspaceKey, "after_tool_result", owner.threadId, request.operation_id);
  expect(sent?.message.content).toBe("输入A");
  expect(sent?.message.attachments?.map(attachment => attachment.name)).toEqual(["a.txt", "b.txt"]);
  expect(sent?.run.delivery_policy).toBe("after_tool_result");
  expect(await readComposerDraft(draftKey)).toBe("");
  expect(await loadMessageSubmissions(owner)).toEqual([]);
});

test("冻结失败不发网络请求，原草稿保持", async () => {
  let requests = 0;
  installSessionApiFetchMock(() => { requests += 1; throw new Error("不应发送"); });
  const database = await openBrowserViewDatabase();
  const transaction = spyOn(database, "transaction").mockImplementation(() => { throw new DOMException("存储已满", "QuotaExceededError"); });
  try {
    await expect(sendUserMessage(PORT, owner.sessionId, "必须保留", "default", [], workspaceKey, "after_turn", owner.threadId))
      .rejects.toThrow("存储已满");
    expect(requests).toBe(0);
  } finally { transaction.mockRestore(); }
});

test("响应丢失按原身份查询并交接，不产生第二个POST", async () => {
  const record = await freezeMessageSubmission(owner, input());
  const methods: string[] = [];
  installSessionApiFetchMock(({ method, path }) => {
    methods.push(method);
    if (method === "POST") throw new TypeError("connection lost after acceptance");
    expect(path.endsWith(`/message-submissions/${encodeURIComponent(record.request.operation_id)}`)).toBe(true);
    return response({ session_id: owner.sessionId, thread_id: owner.threadId, operation_id: record.request.operation_id,
      status: "accepted", accepted: receipt(record.request.operation_id) });
  });
  const accepted = await submitFrozenMessage(PORT, record);
  expect(accepted.job_id).toBe("job_canonical");
  expect(methods).toEqual(["POST", "GET"]);
  expect(await loadMessageSubmissions(owner)).toEqual([]);
});

test("撤销观察后迟到的接收响应不释放尚未交接的输入", async () => {
  const record = await freezeMessageSubmission(owner, input());
  let release!: (value: Response) => void;
  let started!: () => void;
  const requested = new Promise<void>(resolve => { started = resolve; });
  installSessionApiFetchMock(() => {
    started();
    // 模拟 transport 忽略 abort；发布与输入交接仍须服从观察 owner。
    return new Promise<Response>(resolve => { release = resolve; });
  });
  const controller = new AbortController();
  const reconciled = reconcileMessageSubmissions(PORT, owner, controller.signal);
  await requested;
  controller.abort();
  release(response({ session_id: owner.sessionId, thread_id: owner.threadId,
    operation_id: record.request.operation_id, status: "accepted", accepted: receipt(record.request.operation_id) }));
  await expect(reconciled).rejects.toHaveProperty("name", "AbortError");
  expect(await loadMessageSubmissions(owner)).toEqual([record]);
});

test.each([409, 503])("HTTP %i不能确定拒绝，重新打开只查询且保留原输入", async status => {
  const record = await freezeMessageSubmission(owner, input());
  let posts = 0;
  installSessionApiFetchMock(({ method }) => {
    if (method === "POST") { posts += 1; return Response.json({ detail: "结果未知" }, { status }); }
    return unknown(record.request.operation_id);
  });
  await expect(submitFrozenMessage(PORT, record)).rejects.toBeInstanceOf(MessageSubmissionUnknownError);
  const reopened = await loadMessageSubmissions(owner);
  expect(reopened).toEqual([record]);
  await reconcileMessageSubmissions(PORT, owner);
  expect(posts).toBe(1);
  expect(await loadMessageSubmissions(owner)).toEqual([record]);
});

test("拒绝状态保存失败仍保留唯一冻结输入，不向Composer报告可回填的普通失败", async () => {
  const record = await freezeMessageSubmission(owner, input());
  const database = await openBrowserViewDatabase();
  const transaction = spyOn(database, "transaction").mockImplementation(() => { throw new DOMException("存储已满", "QuotaExceededError"); });
  installSessionApiFetchMock(({ method }) => method === "POST"
    ? Response.json({ detail: "输入不合法" }, { status: 422 }) : unknown(record.request.operation_id));
  try {
    await expect(submitFrozenMessage(PORT, record)).rejects.toBeInstanceOf(MessageSubmissionUnknownError);
  } finally { transaction.mockRestore(); }
  expect(await loadMessageSubmissions(owner)).toEqual([record]);
});

test("拒绝输入不在重启时发送；明确重提原子交接，丢弃不影响B及共享引用", async () => {
  const request = input();
  const record = await freezeMessageSubmission(owner, request);
  const shared = await freezeMessageSubmission(owner, { ...request, operation_id: `op_${crypto.randomUUID()}` });
  const draftKey = `submission-draft-${owner.threadId}`;
  await writeComposerDraft(draftKey, "草稿B");
  let requests = 0;
  installSessionApiFetchMock(({ method }) => {
    requests += 1;
    return method === "POST" ? Response.json({ detail: "输入不合法" }, { status: 422 }) : unknown(request.operation_id);
  });
  await expect(submitFrozenMessage(PORT, record)).rejects.toBeInstanceOf(MessageSubmissionRejectedError);
  const rejected = (await loadMessageSubmissions(owner))[0]!;
  expect(rejected.state).toBe("rejected");
  // 另一操作也转为明确拒绝，仅为隔离恢复时不得发网络的断言。
  await rejectMessageSubmission(shared, "另一次输入拒绝");
  requests = 0;
  await reconcileMessageSubmissions(PORT, owner);
  expect(requests).toBe(0);
  const next = await resubmitRejectedMessage(rejected, "明确编辑后的A");
  expect(next.request.operation_id).not.toBe(request.operation_id);
  expect(next.request.message.attachments).toEqual(request.message.attachments);
  expect(next.request.run).toEqual(request.run);
  expect((await loadMessageSubmissions(owner)).map(item => item.request.operation_id)).toEqual([next.request.operation_id, shared.request.operation_id]);
  await releaseMessageSubmission(next);
  expect((await loadMessageSubmissions(owner))[0]!.request.message.attachments).toEqual(shared.request.message.attachments);
  expect(await readComposerDraft(draftKey)).toBe("草稿B");
});

test("原生文件输入只读取一次，结果未知后重试使用冻结字节", async () => {
  class ReadingFile {
    result: string | null = null;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    readAsDataURL(blob: Blob) { void blob.arrayBuffer().then(bytes => {
      this.result = `data:${blob.type};base64,${Buffer.from(bytes).toString("base64")}`; this.onload?.();
    }); }
  }
  Object.defineProperty(globalThis, "FileReader", { configurable: true, value: ReadingFile });
  const operationId = `op_${crypto.randomUUID()}`;
  let reads = 0, posts = 0;
  const bodies: MessageRunRequest[] = [];
  installSessionApiFetchMock(({ path, method, init, url }) => {
    if (path === "/api/v1/workspace/files/raw") {
      reads += 1; expect(new URL(url, "http://127.0.0.1").searchParams.get("path")).toBe("/target/native/a.txt");
      return new Response("文件原输入", { headers: { "Content-Type": "text/plain" } });
    }
    if (method === "POST") {
      posts += 1; bodies.push(JSON.parse(String(init?.body)) as MessageRunRequest);
      return posts === 1 ? Response.json({ detail: "结果未知" }, { status: 503 }) : response(receipt(operationId));
    }
    return unknown(operationId);
  });
  await expect(sendUserMessage(PORT, owner.sessionId, "输入", "default", [{ file_id: "/target/native/a.txt" }],
    workspaceKey, "after_turn", owner.threadId, operationId)).rejects.toBeInstanceOf(MessageSubmissionUnknownError);
  const record = (await loadMessageSubmissions(owner))[0]!;
  const dataUrl = record.request.message.attachments![0]!.data_url!;
  expect(dataUrl).toStartWith("data:text/plain");
  expect(Buffer.from(dataUrl.split(",")[1]!, "base64").toString()).toBe("文件原输入");
  await submitFrozenMessage(PORT, record);
  expect(reads).toBe(1); expect(posts).toBe(2); expect(bodies[1]).toEqual(bodies[0]);
});

test("易失URL、重复身份和非拒绝输入改写均不会覆盖原记录", async () => {
  const request = input();
  const record = await freezeMessageSubmission(owner, request);
  request.message.content = "后来修改";
  expect((await loadMessageSubmissions(owner))[0]!.request.message.content).toBe("输入A");
  await expect(freezeMessageSubmission(owner, request)).rejects.toThrow("已冻结");
  await expect(resubmitRejectedMessage(record, "新内容")).rejects.toThrow("结果未知");
  await expect(freezeMessageSubmission(owner, { ...input(), message: { content: "", attachments: [{ file_id: "blob:temporary" }] } })).rejects.toThrow("临时页面");
  expect(await readBrowserView<unknown>(messageSubmissionKey(owner))).toEqual([record]);
});

test("回执属于另一Session或缺少队列水位时，不能释放唯一冻结输入", async () => {
  const record = await freezeMessageSubmission(owner, input());
  const invalid = receipt(record.request.operation_id);
  invalid.dispatch = { ...invalid.dispatch, session_id: "ses_another" };
  installSessionApiFetchMock(({ method }) => method === "POST" ? response(invalid) : unknown(record.request.operation_id));
  await expect(submitFrozenMessage(PORT, record)).rejects.toBeInstanceOf(MessageSubmissionUnknownError);
  expect(await loadMessageSubmissions(owner)).toEqual([record]);
  invalid.dispatch = { ...invalid.dispatch, session_id: owner.sessionId, queue_snapshot_version: undefined };
  await expect(submitFrozenMessage(PORT, record)).rejects.toBeInstanceOf(MessageSubmissionUnknownError);
  expect(await loadMessageSubmissions(owner)).toEqual([record]);
});
