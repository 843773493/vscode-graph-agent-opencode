import { toJson } from "@bufbuild/protobuf";
import { describe, expect, test } from "bun:test";
import { parseSessionExecutionSse } from "./sessionSse";
import { SessionExecutionSseSchema } from "../types/protocol_buf_generated/boxteam/workspace/v2/session_stream_pb";

describe("Web Session SSE Protobuf adapter", () => {
  test("消息事件保留 child owner 与消息创建时间，独立于事件发布时间", () => {
    const value = {
      event: {
        event_id: "child-message-event",
        session_id: "session-owner",
        type: "message.updated",
        time: "2026-10-14T12:00:00Z",
        payload: {
          message_id: "child-message",
          session_id: "session-owner",
          thread_id: "thread-child",
          role: "user",
          content: "child 输入",
          created_at: "2026-10-14T11:59:00Z",
        },
      },
      raw_type: "message_created",
    };
    const message = parseSessionExecutionSse(value);
    expect(message.event?.payload.case).toBe("messageUpdated");
    if (message.event?.payload.case !== "messageUpdated") throw new Error("消息事件缺少 typed payload");
    expect(message.event.payload.value.threadId).toBe("thread-child");
    expect(toJson(SessionExecutionSseSchema, message)).toMatchObject({
      event: {
        header: { time: "2026-10-14T12:00:00Z" },
        messageUpdated: { threadId: "thread-child", createdAt: "2026-10-14T11:59:00Z" },
      },
    });
    for (const thread_id of [undefined, null, ""]) {
      expect(() => parseSessionExecutionSse({
        ...value,
        event: { ...value.event, payload: { ...value.event.payload, thread_id } },
      })).toThrow("thread_id 必须是非空字符串");
    }
  });

  test("把 job.updated JSON 映射到 typed oneof", () => {
    const message = parseSessionExecutionSse({
      event: {
        event_id: "event-1",
        session_id: "session-1",
        job_id: null,
        type: "job.updated",
        time: "2026-08-24T00:00:00Z",
        payload: {
          job_id: "job-1",
          status: "running",
          progress: 42,
          message: "working",
        },
      },
      raw_type: "job_updated",
      raw_payload: { trace_id: "trace-1" },
    });

    expect(message.event?.payload.case).toBe("jobUpdated");
    expect(toJson(SessionExecutionSseSchema, message)).toEqual({
      event: {
        type: "job.updated",
        header: {
          eventId: "event-1",
          sessionId: "session-1",
          time: "2026-08-24T00:00:00Z",
        },
        jobUpdated: {
          jobId: "job-1",
          status: "JOB_STATUS_RUNNING",
          progress: 42,
          message: "working",
        },
      },
      rawType: "job_updated",
      rawPayload: { trace_id: "trace-1" },
    });
  });

  test("拒绝未知的 Job 状态", () => {
    expect(() =>
      parseSessionExecutionSse({
        event: {
          event_id: "event-1",
          session_id: "session-1",
          type: "job.updated",
          time: "2026-08-24T00:00:00Z",
          payload: {
            job_id: "job-1",
            status: "not-a-status",
          },
        },
        raw_type: "job_updated",
      }),
    ).toThrow("不支持 not-a-status");
  });
});
