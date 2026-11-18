import { expect, test } from "bun:test";
import type { JobDispatchSnapshot, PendingRequestList } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { writeDispatchActiveJob, writePendingSnapshot } from "../conversations/pendingQueue";
import { mergeSubmissionConversation } from "./projection";

const SESSION = "ses_watermark", THREAD = "thr_watermark", SCOPE = "gateway-workspace-session-thread";
function queued(version: number): PendingRequestList {
  return { session_id: SESSION, thread_id: THREAD, active_job_id: null, snapshot_version: version, requests: [{
    session_id: SESSION, thread_id: THREAD, job_id: "job_watermark", message_id: "msg_watermark", turn_id: "turn_watermark",
    content: "排队消息", attachments: [], agent_id: "default", delivery_policy: "after_turn", reorderable: true, enqueue_sequence: 1,
    position: 0, message_created_at: "2026-11-13T00:00:00Z", message_metadata: { updated_at: "2026-11-13T00:00:00Z" },
    created_at: "2026-11-13T00:00:00Z", updated_at: "2026-11-13T00:00:00Z", snapshot_version: version,
    execution_binding: {},
  }] };
}
function started(version: number): JobDispatchSnapshot {
  return { session_id: SESSION, thread_id: THREAD, job_id: "job_watermark", job_status: "running",
    active_job_id: "job_watermark", queued_jobs_ahead: 0, queued_job_count: 0, pending_job_count: 1,
    queue_snapshot_version: version };
}

test("空队列版本12后非空11迟到，水位不依赖现有元素", () => {
  const pending = new Map<string, ConversationView[]>(), active = new Map<string, string>();
  const versions = new Map<string, Map<string, number>>();
  writePendingSnapshot(pending, active, { ...queued(12), requests: [] }, SCOPE, versions);
  writePendingSnapshot(pending, active, queued(11), SCOPE, versions);
  expect(pending.get(SCOPE) ?? []).toEqual([]);
  expect(versions.get(SCOPE)?.get(THREAD)).toBe(12);
});

test("开始执行dispatch推进原队列水位，旧queued不能复活", () => {
  const pending = new Map<string, ConversationView[]>(), active = new Map<string, string>();
  const versions = new Map<string, Map<string, number>>();
  writeDispatchActiveJob(active, started(12), SCOPE, versions);
  writePendingSnapshot(pending, active, queued(11), SCOPE, versions);
  expect(pending.get(SCOPE) ?? []).toEqual([]);
  expect(active.get(SCOPE)).toBe("job_watermark");
  expect(versions.get(SCOPE)?.get(THREAD)).toBe(12);
});

test("即使快照版本更新，同一Job已开始事实仍不退回queued", () => {
  const pending = new Map<string, ConversationView[]>(), active = new Map<string, string>();
  const versions = new Map<string, Map<string, number>>();
  writePendingSnapshot(pending, active, queued(11), SCOPE, versions);
  const live = { ...pending.get(SCOPE)![0]!, status: "running" as const, activeJobOverlay: true };
  pending.set(SCOPE, [live]);
  writeDispatchActiveJob(active, started(12), SCOPE, versions);
  writePendingSnapshot(pending, active, queued(13), SCOPE, versions);
  expect(pending.get(SCOPE)![0]!.status).toBe("running");
  expect(pending.get(SCOPE)![0]!.activeJobOverlay).toBe(true);
  expect(active.get(SCOPE)).toBe("job_watermark");
});

test("冷恢复的终态回执清除原Job活动镜像，不清除后来运行的另一Job", () => {
  const active = new Map([[SCOPE, "job_watermark"]]);
  const versions = new Map<string, Map<string, number>>();
  const terminal: JobDispatchSnapshot = { ...started(13), job_status: "completed", active_job_id: null };
  writeDispatchActiveJob(active, terminal, SCOPE, versions);
  expect(active.has(SCOPE)).toBe(false);
  active.set(SCOPE, "job_new");
  writeDispatchActiveJob(active, terminal, SCOPE, versions);
  expect(active.get(SCOPE)).toBe("job_new");
});

test("迟到的接收回执不覆盖更晚的执行投影或终态，确认终态仍能覆盖running", () => {
  const pending = new Map<string, ConversationView[]>(), active = new Map<string, string>();
  writePendingSnapshot(pending, active, queued(11), SCOPE, new Map());
  const original = pending.get(SCOPE)![0]!;
  const live = { ...original, status: "running" as const, queueSnapshotVersion: 13, activeJobOverlay: true,
    pendingSubmissionId: "op_watermark" };
  const receipt = { ...original, pendingSubmissionId: "op_watermark" };
  let merged = mergeSubmissionConversation([live], receipt);
  expect(merged).toHaveLength(1);
  expect(merged[0]!.status).toBe("running");
  expect(merged[0]!.queueSnapshotVersion).toBe(13);
  const terminal = { ...receipt, status: "done" as const, pending: false, queueSnapshotVersion: 14, activeJobOverlay: false };
  merged = mergeSubmissionConversation(merged, terminal);
  expect(merged[0]!.status).toBe("done");
  expect(merged[0]!.pending).toBe(false);
  expect(mergeSubmissionConversation(merged, receipt)[0]!.status).toBe("done");
});
