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


test("Gateway 完整组请求、blocker 与两种客户端绑定往返保持成员和 generation", async () => {
  const bindings = await Promise.all([
    import("../../src/workspace-services/protocol/generated/boxteam/gateway/v1/public_pb.js"),
    import("../../src/clients/web/src/types/protocol_buf_generated/boxteam/gateway/v1/public_pb.js"),
  ]);
  for (const binding of bindings) {
    const schema = binding.GatewayRuntimeGroupRequestSchema;
    const request = create(schema, {
      expectedBackendGeneration: "generation-current",
      expectedWorkspaceIds: ["gateway-a", "gateway-b"],
      desiredWorkspaceIds: { workspaceIds: ["gateway-a", "gateway-c"] },
    });
    const decoded = fromBinary(schema, toBinary(schema, request));
    expect(decoded.expectedBackendGeneration).toBe("generation-current");
    expect(decoded.expectedWorkspaceIds).toEqual(["gateway-a", "gateway-b"]);
    expect(decoded.desiredWorkspaceIds.workspaceIds).toEqual(["gateway-a", "gateway-c"]);
    expect(create(schema).desiredWorkspaceIds).toBeUndefined();
    const empty = create(schema, {
      expectedBackendGeneration: "generation-current", expectedWorkspaceIds: ["gateway-a"],
      desiredWorkspaceIds: { workspaceIds: [] },
    });
    const decodedEmpty = fromBinary(schema, toBinary(schema, empty));
    expect(decodedEmpty.desiredWorkspaceIds).toBeDefined();
    expect(decodedEmpty.desiredWorkspaceIds.workspaceIds).toEqual([]);
    expect(create(schema).expectedBackendGeneration).toBeUndefined();
    for (const schema of [
      binding.GatewayRuntimeRestartResultDTOSchema,
      binding.GatewayRuntimeStateResultDTOSchema,
      binding.GatewayWorkspaceDTOSchema,
      binding.FederationWorkspaceDTOSchema,
    ]) {
      const value = create(schema, {
        backendGeneration: "generation-current",
        affectedWorkspaceIds: ["gateway-a", "gateway-b"],
      });
      const decoded = fromBinary(schema, toBinary(schema, value));
      expect(decoded.backendGeneration).toBe("generation-current");
      expect(decoded.affectedWorkspaceIds).toEqual(["gateway-a", "gateway-b"]);
      expect(create(schema).backendGeneration).toBeUndefined();
    }
    const value = create(binding.GatewayRuntimeRestartResultDTOSchema, {
      status: "blocked", backendGeneration: "generation-current",
      affectedWorkspaceIds: ["gateway-a", "gateway-b"],
      blockers: [{ kind: "proxy_stream", workspaceId: "gateway-b", resourceId: "route-b", status: "active" }],
    });
    const decodedResult = fromBinary(
      binding.GatewayRuntimeRestartResultDTOSchema,
      toBinary(binding.GatewayRuntimeRestartResultDTOSchema, value),
    );
    expect(decodedResult.blockers[0].workspaceId).toBe("gateway-b");
    expect(decodedResult.status).toBe("blocked");
  }
});
