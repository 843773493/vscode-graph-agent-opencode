import { create } from "@bufbuild/protobuf";
import { test, expect } from "bun:test";
import {
  SessionExecutionEventSchema,
} from "../../src/workspace-services/protocol/generated/boxteam/workspace/v2/session_interaction_pb.js";

test("Node Protobuf 生成结果保留跨文件 import 与 oneof", () => {
  const event = create(SessionExecutionEventSchema, {
    type: "job.updated",
    header: {
      eventId: "event_123",
      sessionId: "session_123",
    },
    payload: {
      case: "jobUpdated",
      value: {
        jobId: "job_123",
        status: 2,
        progress: 42,
      },
    },
  });

  expect(event.payload.case).toBe("jobUpdated");
  expect(event.payload.value.progress).toBe(42);
});
