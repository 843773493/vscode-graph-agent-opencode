import { toJson } from "@bufbuild/protobuf";
import { describe, expect, test } from "bun:test";
import { parseSessionExecutionSse } from "./sessionSse";
import { SessionExecutionSseSchema } from "../types/protocol_buf_generated/boxteam/workspace/v2/session_stream_pb";

describe("Web Session SSE Protobuf adapter", () => {
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
