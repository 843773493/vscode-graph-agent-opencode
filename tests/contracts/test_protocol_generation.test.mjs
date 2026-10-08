import { create, fromBinary, toBinary } from "@bufbuild/protobuf";
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


test("公开诊断字段没有真实定位，通信目标只保留一个 destination", async () => {
  const {
    CommunicationGatewayRequestSchema,
    SessionInformationSnapshotDTOSchema,
    SessionInformationWorkspaceDTOSchema,
  } = await import("../../src/workspace-services/protocol/generated/boxteam/workspace/v2/public_pb.js");
  expect(SessionInformationSnapshotDTOSchema.fields.map((field) => field.name)).not.toContain("storage_path");
  expect(SessionInformationWorkspaceDTOSchema.fields.map((field) => field.name)).not.toContain("root_path");
  const target = create(CommunicationGatewayRequestSchema, {
    operation: "read", destination: {
      case: "resourceTarget",
      value: { gatewayId: "gateway-c", resourceIdentity: "resource", uri: "resource-uri", scope: "workspace" },
    },
  });
  const decoded = fromBinary(CommunicationGatewayRequestSchema, toBinary(CommunicationGatewayRequestSchema, target));
  expect(decoded.destination.case).toBe("resourceTarget");
  expect(decoded.destination.value.gatewayId).toBe("gateway-c");
});
