import "fake-indexeddb/auto";
import React from "react";
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "../../api/session/sessionApiFetchMock";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { MessageRunAccepted, MessageRunRequest } from "../../types/backend";
import { installComposerTestGlobals } from "./composerTestFixtures";
import MessageSubmissionRecovery from "./MessageSubmissionRecovery";
import {
  freezeMessageSubmission,
  loadMessageSubmissions,
  releaseMessageSubmission,
  type MessageSubmissionOwner,
} from "../../state/messageSubmissions/storage";

const PORT = 54_621;
const OWNER_A = {
  workspaceKey: gatewayWorkspaceKey({ gatewayId: "gateway-recovery-a", workspaceId: "00000000-0000-4000-8000-000000000081" }),
  sessionId: "ses_recovery",
  threadId: "thr_recovery",
};
const OWNER_B = {
  workspaceKey: gatewayWorkspaceKey({ gatewayId: "gateway-recovery-b", workspaceId: "00000000-0000-4000-8000-000000000082" }),
  sessionId: "ses_recovery",
  threadId: "thr_recovery",
};
const originalOperationId = "op_original_recovery";
const requests: Array<{ method: string; path: string; body?: MessageRunRequest }> = [];
let restoreComposerGlobals: () => void = () => {};

function input(content: string, operationId: string): MessageRunRequest {
  return {
    operation_id: operationId,
    message: { role: "user", content, attachments: [], metadata: {} },
    run: { mode: "single_agent", agent_id: "default", response_mode: "stream", async: true, delivery_policy: "after_turn" },
  };
}

function receipt(owner: MessageSubmissionOwner, operationId: string): MessageRunAccepted {
  return {
    operation_id: operationId,
    thread_id: owner.threadId,
    message_id: "msg_recovery",
    job_id: "job_recovery",
    turn_id: "turn_recovery",
    status: "queued",
    dispatch: {
      session_id: owner.sessionId,
      thread_id: owner.threadId,
      job_id: "job_recovery",
      job_status: "queued",
      queued_jobs_ahead: 0,
      queued_job_count: 1,
      pending_job_count: 1,
      queue_snapshot_version: 2,
    },
  };
}

function response(data: unknown, status = 200): Response {
  return Response.json({ code: status === 200 ? 0 : status, request_id: "req_recovery", data }, { status });
}

async function settle(): Promise<void> {
  await new Promise<void>(resolve => setTimeout(resolve, 0));
}

function findButton(renderer: ReactTestRenderer, label: string) {
  return renderer.root.findAllByType("button").find(button => button.children.includes(label));
}

afterEach(async () => {
  unwrapSessionApiFetch();
  restoreComposerGlobals();
  restoreComposerGlobals = () => undefined;
  for (const owner of [OWNER_A, OWNER_B]) {
    for (const record of await loadMessageSubmissions(owner)) await releaseMessageSubmission(record);
  }
  requests.length = 0;
});

test("恢复控件按 workspace/session/thread 隔离，并以原 operation_id 重试后再查询", async () => {
  restoreComposerGlobals = installComposerTestGlobals({ port: PORT });
  const frozen = await freezeMessageSubmission(OWNER_A, input("A 的冻结输入", originalOperationId));
  await freezeMessageSubmission(OWNER_B, input("B 的其它工作区输入", "op_other_workspace"));
  let queryAttempt = 0;
  let acceptedRefreshes = 0;
  installSessionApiFetchMock(({ method, path, init }) => {
    const body = method === "POST" ? JSON.parse(String(init?.body)) as MessageRunRequest : undefined;
    requests.push({ method, path, ...(body ? { body } : {}) });
    if (method === "POST" && path === `/api/v1/sessions/${OWNER_A.sessionId}/threads/${OWNER_A.threadId}/messages`) {
      expect(body).toEqual(frozen.request);
      return Response.json({ detail: "发送响应未收到" }, { status: 503 });
    }
    if (method === "GET" && path.endsWith(`/message-submissions/${originalOperationId}`)) {
      queryAttempt += 1;
      return queryAttempt === 1
        ? response({ session_id: OWNER_A.sessionId, thread_id: OWNER_A.threadId, operation_id: originalOperationId, status: "unknown", accepted: null })
        : response({ session_id: OWNER_A.sessionId, thread_id: OWNER_A.threadId, operation_id: originalOperationId, status: "accepted", accepted: receipt(OWNER_A, originalOperationId) });
    }
    return undefined;
  });

  let renderer: ReactTestRenderer | undefined;
  try {
    expect(await loadMessageSubmissions(OWNER_A)).toEqual([frozen]);
    await act(async () => {
      renderer = create(<MessageSubmissionRecovery owner={OWNER_A} port={PORT} onAccepted={() => { acceptedRefreshes += 1; }} />);
      await settle();
      await settle();
    });
    if (!renderer) throw new Error("恢复控件未挂载");
    expect(JSON.stringify(renderer.toJSON())).toContain("A 的冻结输入");
    expect(JSON.stringify(renderer.toJSON())).not.toContain("B 的其它工作区输入");
    expect(requests).toEqual([]);

    const retry = findButton(renderer, "按原身份重试");
    if (!retry) throw new Error("恢复控件缺少原身份重试按钮");
    await act(async () => { retry.props.onClick(); await settle(); await settle(); });

    expect(requests.map(request => request.method)).toEqual(["POST", "GET"]);
    expect(requests[0]?.body?.operation_id).toBe(originalOperationId);
    expect(requests[0]?.body?.message.content).toBe("A 的冻结输入");
    expect(await loadMessageSubmissions(OWNER_A)).toEqual([frozen]);
    expect(JSON.stringify(renderer.toJSON())).toContain("消息接收结果待确认，原输入已保存");

    const query = findButton(renderer, "查询接收结果");
    if (!query) throw new Error("恢复控件缺少结果查询按钮");
    await act(async () => { query.props.onClick(); await settle(); await settle(); await settle(); });

    expect(requests.map(request => request.method)).toEqual(["POST", "GET", "GET"]);
    expect(requests.slice(1).every(request => request.path.endsWith(`/message-submissions/${originalOperationId}`))).toBe(true);
    expect(queryAttempt).toBe(2);
    expect(await loadMessageSubmissions(OWNER_A)).toEqual([]);
    expect(acceptedRefreshes).toBe(1);
  } finally {
    if (renderer) act(() => renderer?.unmount());
  }
});
